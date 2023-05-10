from __future__ import annotations
from datetime import datetime
from uuid import uuid4
from time import sleep
import json
from typing import List, Optional

from pydantic import ValidationError

import faiss
from langchain.chat_models import ChatOpenAI
from langchain.vectorstores import FAISS
from langchain.docstore import InMemoryDocstore
from langchain.agents import Tool
from langchain.tools import StructuredTool
from langchain.tools.human.tool import HumanInputRun
from langchain.vectorstores.base import VectorStoreRetriever
from langchain.vectorstores import Chroma
from langchain.embeddings import OpenAIEmbeddings
from langchain.chains.llm import LLMChain
from langchain.schema import (
    AIMessage,
    BaseMessage,
    Document,
    HumanMessage,
    SystemMessage,
)

from yeager_core.agents.yeager_autogpt.output_parser import AutoGPTOutputParser
from yeager_core.agents.yeager_autogpt.prompt import AutoGPTPrompt
from yeager_core.agents.yeager_autogpt.prompt_generator import FINISH_NAME
from yeager_core.sockets.world_socket_client import WorldSocketClient
from yeager_core.agents.yeager_autogpt.listening_antenna import ListeningAntenna
from yeager_core.events.base_event import EventHandler, EventDict
from yeager_core.properties.basic_properties import Coordinates, Size
from yeager_core.events.basic_events import (
    AgentGetsNearbyEntitiesEvent,
    AgentGetsObjectInfoEvent,
    AgentGetsAgentInfoEvent,
    AgentSpeaksWithAgentEvent,
    EntityRequestWorldStateUpdateEvent,
)


class YeagerAutoGPT:
    """Agent class for interacting with Auto-GPT."""

    def __init__(
        self,
        ai_name: str,
        description: str,
        goals: List[str],
        important_event_types: List[str],
        event_dict: EventDict,
        event_handler: EventHandler,
        vision_radius: int,
        openai_api_key: str,
        feedback_tool: Optional[HumanInputRun] = None,
        additional_memories: Optional[List[VectorStoreRetriever]] = None,
    ):
        # Its own properties
        self.id = str(uuid4())
        self.ai_name = ai_name
        self.description = description
        self.goals = goals
        self.world_spawned_id = None

        # Event properties
        self.important_event_types = important_event_types
        important_event_types.extend(
            [
                "agent_gets_nearby_entities_event",
                "world_sends_nearby_entities_event",
                "agent_gets_object_info",
                "agent_gets_agent_info",
                "agent_interacts_with_object",
                "agent_interacts_with_agent",
            ]
        )

        self.event_dict = event_dict

        # Phisical world properties
        self.vision_radius = vision_radius
        self.world_socket_client = WorldSocketClient(process_event=print)
        self.listening_antenna = ListeningAntenna(
            self.important_event_types,
            agent_name=self.ai_name,
            agent_id=self.id,
        )

        # Agent actions
        self.actions = [
            StructuredTool.from_function(
                name="agent_gets_nearby_entities_event",
                description="Gets nearby entities",
                func=self.agent_gets_nearby_entities_action,
            ),
            StructuredTool.from_function(
                name="get_object_info",
                description="Gets the info of an object.",
                func=self.agent_gets_object_info_action,
            ),
            StructuredTool.from_function(
                name="get_agent_info",
                description="Gets the info of an agent.",
                func=self.agent_gets_agent_info_action,
            ),
            StructuredTool.from_function(
                name="interact_with_object",
                description="Interacts with an object.",
                func=self.agent_interacts_with_object_action,
            ),
        ]

        # Brain properties
        self.embeddings_model = OpenAIEmbeddings(openai_api_key=openai_api_key)
        embedding_size = 1536
        index = faiss.IndexFlatL2(embedding_size)
        vectorstore = FAISS(
            self.embeddings_model.embed_query, index, InMemoryDocstore({}), {}
        )
        self.memory = vectorstore.as_retriever()

        llm = ChatOpenAI(openai_api_key=openai_api_key, model_name="gpt-4")
        prompt = AutoGPTPrompt(
            ai_name=self.ai_name,
            ai_role=self.description,
            vision_radius=self.vision_radius,
            tools=self.actions,
            input_variables=["memory", "messages", "goals", "user_input", "schemas", "plan", "agent_world_state"],
            token_counter=llm.get_num_tokens,
        )
        print(prompt.construct_full_prompt("Default world state", []))
        self.chain = LLMChain(llm=llm, prompt=prompt)

        self.full_message_history: List[BaseMessage] = []
        self.next_action_count = 0
        self.output_parser = AutoGPTOutputParser()
        self.feedback_tool = None  # HumanInputRun() if human_in_the_loop else None
        self.schemas_memory : Chroma
        self.plan: Optional[str] = None

    def think(self):
        print(f" The agent {self.ai_name} is thinking...")
        user_input = (
            "Determine which next command to use, "
            "and respond using the format specified above:"
        )
        sleep(20)
        self.schemas_memory = Chroma.from_documents(self.listening_antena.schemas_as_docs, self.embeddings_model)
        # Get the initial world state
        self.agent_request_world_state_update_action()
        sleep(1)

        while True:
            agent_world_state = self.listening_antenna.get_agent_world_state()

            # Send message to AI, get response
            if self.plan:
                useful_schemas = self.schemas_memory.similarity_search(self.plan)
            else:
                useful_schemas = [""]
            assistant_reply = self.chain.run(
                goals=self.goals,
                messages=self.full_message_history,
                memory=self.memory,
                schemas=useful_schemas,
                plan=self.plan,
                user_input=user_input,
                agent_world_state=agent_world_state,
            )
            self.plan = json.loads(assistant_reply)["thoughts"]["plan"]
            # Print Assistant thoughts
            print(assistant_reply) # Send the thoughts as events
            self.full_message_history.append(HumanMessage(content=user_input))
            self.full_message_history.append(AIMessage(content=assistant_reply))

            # Get command name and arguments
            action = self.output_parser.parse(assistant_reply)
            tools = {t.name: t for t in self.actions}
            if action.name == FINISH_NAME:
                return action.args["response"]
            if action.name in tools:
                tool = tools[action.name]
                try:
                    observation = tool.run(action.args)
                except ValidationError as e:
                    observation = (
                        f"Validation Error in args: {str(e)}, args: {action.args}"
                    )
                except Exception as e:
                    observation = (
                        f"Error: {str(e)}, {type(e).__name__}, args: {action.args}"
                    )
                result = f"Command {tool.name} returned: {observation}"
            elif action.name == "ERROR":
                result = f"Error: {action.args}. "
            else:
                result = (
                    f"Unknown command '{action.name}'. "
                    f"Please refer to the 'COMMANDS' list for available "
                    f"commands and only respond in the specified JSON format."
                )
            ## send result and assistant_reply to the socket
            print(result)

            # If there are any relevant events in the world for this agent, add them to memory
            sleep(3)
            last_events = self.listening_antenna.get_last_events()
            memory_to_add = (
                f"Assistant Reply: {assistant_reply} "
                f"\nResult: {result} "
                f"\nLast World Events: {last_events}"
            )

            print(f"Adding to memory: {memory_to_add}")

            if self.feedback_tool is not None:
                feedback = f"\n{self.feedback_tool.run('Input: ')}"
                if feedback in {"q", "stop"}:
                    print("EXITING")
                    return "EXITING"
                memory_to_add += feedback

            self.memory.add_documents([Document(page_content=memory_to_add)])
            self.full_message_history.append(SystemMessage(content=result))


    def agent_gets_nearby_entities_action(self):
        agent_gets_nearby_entities_event = AgentGetsNearbyEntitiesEvent(
            created_at=datetime.now(),
            agent_id=self.id,
            world_id=self.world_spawned_id,
        )
        self.world_socket_client.send_message(agent_gets_nearby_entities_event.json())

    def agent_gets_object_info_action(
        self,
        object_id: str,
    ):
        agent_gets_object_info = AgentGetsObjectInfoEvent(
            created_at=datetime.now(),
            agent_id=self.id,
            object_id=object_id,
        )
        self.world_socket_client.send_message(agent_gets_object_info.json())

    def agent_gets_agent_info_action(
        self,
        agent_id: str,
    ):
        agent_gets_agent_info = AgentGetsAgentInfoEvent(
            created_at=datetime.now(),
            agent_id=self.id,
            other_agent_id=agent_id,
        )
        self.world_socket_client.send_message(agent_gets_agent_info.json())

    def agent_interacts_with_object_action(
        self,
        created_interaction: str,
    ):
        self.world_socket_client.send_message(created_interaction.json())

    def agent_speaks_with_agent_action(
        self,
        other_agent_id: str,
        message: str,
    ):
        agent_speaks_with_agent = AgentSpeaksWithAgentEvent(
            created_at=datetime.now(),
            agent_id=self.id,
            other_agent_id=other_agent_id,
            message=message,
        )
        self.world_socket_client.send_message(agent_speaks_with_agent.json())

    def agent_request_world_state_update_action(self):
        agent_request_world_state_update = EntityRequestWorldStateUpdateEvent(
            created_at=datetime.now(),
            entity_id=self.id,
        )
        self.world_socket_client.send_message(agent_request_world_state_update.json())
