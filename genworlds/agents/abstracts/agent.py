from abc import abstractmethod
from time import sleep
import threading

from genworlds.agents.utils.validate_action import validate_action
from genworlds.agents.abstracts.action_planner import AbstractActionPlanner
from genworlds.agents.abstracts.state_manager import AbstractStateManager
from genworlds.objects.abstracts.object import AbstractObject

class AbstractAgent(AbstractObject):
    """Abstract Base Class for an Agent.
    
    This class represents an abstract agent that can think and perform actions
    within a simulation environment.
    """

    @property
    @abstractmethod
    def state_manager(self) -> AbstractStateManager:
        """Property that should return the State Manager instance associated with the agent."""
        pass

    @property
    @abstractmethod
    def action_planner(self) -> AbstractActionPlanner:
        """Property that should return the Action Planner instance associated with the agent."""
        pass
    
    def think_n_do(self):
        """Continuously plans and executes actions based on the agent's state."""
        while True:
            try:
                state = self.state_manager.get_updated_state()
                action_schema, pre_filled_event = self.action_planner.plan_next_action(state)
                is_my_action, trigger_event = validate_action(agent_id = self.id,
                                                        action_schema=action_schema, 
                                                        pre_filled_event=pre_filled_event,
                                                        all_action_schemas=state.available_action_schemas)
                if is_my_action:
                    selected_action = self.actions[self.actions.index(action_schema)]
                    selected_action(trigger_event)                
                else:
                    self.send_event(trigger_event)

            except Exception as e:
                print(f"Error in think_n_do: {e}")
    
    def launch(self):
        """Launches the agent by starting the websocket and thinking threads."""
        self.launch_websocket_thread()
        sleep(0.1)
        thinking_thread = threading.Thread(
            target=self.think_n_do,
            name=f"Agent {self.state_manager.state.id} Thinking Thread",
            daemon=True,
        )
        thinking_thread.start()
