# ruff: noqa: E722
import json
import traceback
import copy
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Union, Callable, Literal, Tuple
from autogen import Agent, ConversableAgent, GroupChatManager, GroupChat, OpenAIWrapper
from autogen.code_utils import extract_code


class Orchestrator(ConversableAgent):
    def __init__(
        self,
        name: str,
        agents: List[ConversableAgent] = [],
        is_termination_msg: Optional[Callable[[Dict], bool]] = None,
        max_consecutive_auto_reply: Optional[int] = None,
        human_input_mode: Optional[str] = "TERMINATE",
        function_map: Optional[Dict[str, Callable]] = None,
        code_execution_config: Union[Dict, Literal[False]] = False,
        llm_config: Optional[Union[Dict, Literal[False]]] = False,
        default_auto_reply: Optional[Union[str, Dict, None]] = "",
    ):
        super().__init__(
            name=name,
            system_message="",
            is_termination_msg=is_termination_msg,
            max_consecutive_auto_reply=max_consecutive_auto_reply,
            human_input_mode=human_input_mode,
            function_map=function_map,
            code_execution_config=code_execution_config,
            llm_config=llm_config,
            default_auto_reply=default_auto_reply,
        )

        self._agents = agents
        self._checkpoints = []

        self.orchestrated_messages = []

        # NOTE: Async reply functions are not yet supported with this contrib agent
        self._reply_func_list = []
        self.register_reply([Agent, None], Orchestrator.run_chat)
        self.register_reply([Agent, None], ConversableAgent.generate_code_execution_reply)
        self.register_reply([Agent, None], ConversableAgent.generate_function_call_reply)
        self.register_reply([Agent, None], ConversableAgent.check_termination_and_human_reply)

    def _print_thought(self, message):
        print(self.name + " (thought)\n")
        print(message.strip() + "\n")
        print("\n", "-" * 80, flush=True, sep="")

    def _broadcast(self, message, out_loud=[], exclude=[]):
        m = copy.deepcopy(message)
        m["role"] = "user"
        for a in self._agents:
            if a in exclude or a.name in exclude:
                continue
            if a in out_loud or a.name in out_loud:
                self.send(message, a, request_reply=False, silent=False)
            else:
                self.send(message, a, request_reply=False, silent=True)

    def save_checkpoint(self, plan, evaluation, response):
        checkpoint = {
            "plan": plan,
            "evaluation": evaluation,
            "response": response,
        }
        self._checkpoints.append(checkpoint)

    def run_chat(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[OpenAIWrapper] = None,
    ) -> Tuple[bool, Union[str, Dict, None]]:
        # We should probably raise an error in this case.
        if self.client is None:
            return False, None

        if messages is None:
            messages = self._oai_messages[sender]

        # Work with a copy of the messages
        _messages = copy.deepcopy(messages)
        
        ##### Memory ####

        # Pop the last message, which is the task
        task = _messages.pop()["content"]
   
        # A reusable description of the team
        team = "\n".join([a.name + ": " + a.description for a in self._agents])
        names = ", ".join([a.name for a in self._agents])

        execution_enabled_agents = [a for a in self._agents if isinstance(a._code_execution_config, dict)]

        # A place to store relevant facts
        facts = ""

        # A place to store the plan
        plan = ""

        #################

        # Start by writing what we know
        closed_book_prompt = f"""Below I will present you a request. Before we begin addressing the request, please answer the following pre-survey to the best of your ability. Keep in mind that you are Ken Jennings-level with trivia, and Mensa-level with puzzles, so there should be a deep well to draw from.

Here is the request:

{task}

Here is the pre-survey:

    1. Please list any specific facts or figures that are GIVEN in the request itself. It is possible that there are none.
    2. Please list any facts that may need to be looked up, and WHERE SPECIFICALLY they might be found. In some cases, authoritative sources are mentioned in the request itself.
    3. Please list any facts that may need to be derived (e.g., via logical deduction, simulation, or computation)
    4. Please list any facts that are recalled from memory, hunches, well-reasoned guesses, etc.

When answering this survey, keep in mind that "facts" will typically be specific names, dates, statistics, etc. Your answer should use headings:

    1. GIVEN OR VERIFIED FACTS
    2. FACTS TO LOOK UP
    3. FACTS TO DERIVE
    4. EDUCATED GUESSES
""".strip()

        _messages.append({"role": "user", "content": closed_book_prompt, "name": sender.name})

        response = self.client.create(
            messages=_messages,
            cache=self.client_cache,
        )
        extracted_response = self.client.extract_text_or_completion_object(response)[0]
        _messages.append({"role": "assistant", "content": extracted_response, "name": self.name})
        facts = extracted_response

        # Make an initial plan
        plan_prompt = f"""Fantastic. To address this request we have assembled the following team:

{team}

Based on the team composition, and known and unknown facts, please devise a short bullet-point plan for addressing the original request. Remember, there is no requirement to involve all team members -- a team member's particular expertise may not be needed for this task.""".strip()
        _messages.append({"role": "user", "content": plan_prompt, "name": sender.name})

        response = self.client.create(
            messages=_messages,
            cache=self.client_cache,
        )

        extracted_response = self.client.extract_text_or_completion_object(response)[0]
        _messages.append({"role": "assistant", "content": extracted_response, "name": self.name})
        plan = extracted_response

        # Main loop
        total_turns = 0
        max_turns = 30
        times_stalled = 0
        while total_turns < max_turns:

            # Populate the message histories
            self.orchestrated_messages = []
            for a in self._agents:
                a.reset()

            self.orchestrated_messages.append({"role": "assistant", "content": f"""
We are working to address the following user request:

{task}


To answer this request we have assembled the following team:

{team}

Some additional points to consider:

{facts}

{plan}
""".strip(), "name": self.name})
            self._broadcast(self.orchestrated_messages[-1])
            self._print_thought(self.orchestrated_messages[-1]["content"])

            # Inner loop
            stalled_count = 0
            while total_turns < max_turns:
                total_turns += 1

                prev_message = self.orchestrated_messages[-1]["content"]
                code_blocks = [t for t in extract_code(prev_message) if t[0] in ["python", "sh"]]

                data = None
                if len(code_blocks) > 0 and len(execution_enabled_agents) > 0:
                    step_prompt = f"""
Recall we are working on the following request:

{task}

To make progress on the request, please answer the following questions, including necessary reasoning:

    - Is the request fully satisfied? (True if complete, or False if the original request has yet to be SUCCESSFULLY addressed)
    - Are we making forward progress? (True if just starting, or recent messages are adding value. False if recent messages show evidence of being stuck in a reasoning or action loop, or there is evidence of significant barriers to success such as the inability to read from a required file)
    - How would you rate the current evaluation of the request? (1 to 100, with 100 being the best possible evaluation)
    
Please output an answer in pure JSON format according to the following schema. The JSON object must be parsable as-is. DO NOT OUTPUT ANYTHING OTHER THAN JSON, AND DO NOT DEVIATE FROM THIS SCHEMA:

    {{
        "is_request_satisfied": {{
            "reason": string,
            "answer": boolean
        }},
        "is_progress_being_made": {{
            "reason": string,
            "answer": boolean
        }},
        "current_evaluation": {{
            "reason": string,
            "answer": int  # Rating from 1 to 100
        }}
    }}
""".strip()

                    # This is a temporary message we will immediately pop
                    self.orchestrated_messages.append({"role": "user", "content": step_prompt, "name": sender.name})
                    response = self.client.create(
                        messages=self.orchestrated_messages,
                        cache=self.client_cache,
                        response_format={"type": "json_object"},
                    )
                    self.orchestrated_messages.pop()

                    extracted_response = self.client.extract_text_or_completion_object(response)[0]
                    try:
                        data = json.loads(extracted_response)
                        data["next_speaker"] = {
                            "reason": "Assigning to an agent that can execute the script.",
                            "answer": execution_enabled_agents[0].name,
                        }
                        data["instruction_or_question"] = {
                            "reason": "Assigning to an agent that can execute the script.",
                            "answer": "Please execute the above script."
                        }
                    except json.decoder.JSONDecodeError as e:
                        # Something went wrong. Restart this loop.
                        self._print_thought(str(e))
                        break
                else:
                    step_prompt = f"""
Recall we are working on the following request:

{task}

And we have assembled the following team:

{team}

To make progress on the request, please answer the following questions, including necessary reasoning:

    - Is the request fully satisfied? (True if complete, or False if the original request has yet to be SUCCESSFULLY addressed)
    - Are we making forward progress? (True if just starting, or recent messages are adding value. False if recent messages show evidence of being stuck in a reasoning or action loop, or there is evidence of significant barriers to success such as the inability to read from a required file)
    - How would you rate the current evaluation of the request? (1 to 100, with 100 being the best possible evaluation)
    - Who should speak next? (select from: {names})
    - What instruction or question would you give this team member? (Phrase as if speaking directly to them, and include any specific information they may need)

Please output an answer in pure JSON format according to the following schema. The JSON object must be parsable as-is. DO NOT OUTPUT ANYTHING OTHER THAN JSON, AND DO NOT DEVIATE FROM THIS SCHEMA:

    {{
        "is_request_satisfied": {{
            "reason": string,
            "answer": boolean
        }},
        "is_progress_being_made": {{
            "reason": string,
            "answer": boolean
        }},
        "current_evaluation": {{
            "reason": string,
            "answer": int  # Rating from 1 to 100
        }},
        "next_speaker": {{
            "reason": string,
            "answer": string (select from: {names})
        }},
        "instruction_or_question": {{
            "reason": string,
            "answer": string
        }}
    }}
""".strip()
                    # This is a temporary message we will immediately pop
                    self.orchestrated_messages.append({"role": "user", "content": step_prompt, "name": sender.name})
                    response = self.client.create(
                        messages=self.orchestrated_messages,
                        cache=self.client_cache,
                        response_format={"type": "json_object"},
                    )
                    self.orchestrated_messages.pop()

                    extracted_response = self.client.extract_text_or_completion_object(response)[0]
                    try:
                        data = json.loads(extracted_response)
                    except json.decoder.JSONDecodeError as e:
                        # Something went wrong. Restart this loop.
                        self._print_thought(str(e))
                        break

                    # Processing the previous message and evaluation
                    prev_message = data["instruction_or_question"]["answer"]
                    prev_evaluation = str(data["current_evaluation"]["answer"])  # Assuming a method to extract current evaluation
                    self._print_thought("\n\nEvaluating and updating plan based on new data...\n\n")

                    # Formulate new plan prompt
                    new_plan_prompt = f"""
                    Based on the most recent feedback and our need for innovation:
                    - Last Evaluation: {prev_evaluation}
                    - Last Response: {json.dumps(data, indent=4)}

                    Please revise our existing plan to develop a more effective approach. The goal is to create a unique plan that diverges from previous strategies, ensuring a fresh perspective. The previous plan was:
                    {plan}

                    To guide your proposal, use bullet points to outline the new plan. Consider the team composition and incorporate insights from the recent evaluation:
                    Team Membership:
                    {team}
                    """.strip()

                    self.orchestrated_messages.append({"role": "user", "content": new_plan_prompt, "name": sender.name})
                    response = self.client.create(
                        messages=self.orchestrated_messages,
                        cache=self.client_cache,
                    )
                    new_plan = self.client.extract_text_or_completion_object(response)[0]
                    self.orchestrated_messages.pop() # Remove new plan prompt
                    # Prepare a message with the new plan to broadcast to all agents
                    new_plan_message = {
                        "role": "user",
                        "content": f"New Updated Plan: \n{new_plan}",
                        "name": self.name
                    }

                    # Broadcast the new plan message to all agents
                    self._broadcast(new_plan_message)  # Assuming self._agents lists all agents who should hear the update

                    self._print_thought(f"Updated Plan:\n{new_plan}\nDONE UPDATED PLAN")

                    # Rerun now with new plan
                    self.orchestrated_messages.append({"role": "user", "content": step_prompt, "name": sender.name})
                    response = self.client.create(
                        messages=self.orchestrated_messages,
                        cache=self.client_cache,
                        response_format={"type": "json_object"},
                    )
                    self.orchestrated_messages.pop()

                    extracted_response = self.client.extract_text_or_completion_object(response)[0]
                    try:
                        data2 = json.loads(extracted_response)
                    except json.decoder.JSONDecodeError as e:
                        # Something went wrong. Restart this loop.
                        self._print_thought(str(e))
                        break

                    self._print_thought("INSTRUCTION1: " + str(data["instruction_or_question"]["answer"]) + "\nINSTRUCTION2: " + str(data2["instruction_or_question"]["answer"]) + "\n\n")
                    self._print_thought("EVAL1: " + str(data["current_evaluation"]["answer"]) + "\nEVAL2: " + str(data2["current_evaluation"]["answer"]) + "\n\n")
                    
                    # Maintain the best evaluation
                    if data2["current_evaluation"]["answer"] > data["current_evaluation"]["answer"]:
                        data = data2
                        plan = new_plan

                # Checkpoint Evaluation
                self.save_checkpoint(plan, data["current_evaluation"]["answer"], data["instruction_or_question"]["answer"])

                self._print_thought("CHECKPOINTS:\n\n" + str(self._checkpoints) + "\n\n" + "DONE CHECKPOINTS")

                self._print_thought(json.dumps(data, indent=4))

                if data["is_request_satisfied"]["answer"]:
                    return True, "TERMINATE"

                if data["is_progress_being_made"]["answer"]:
                    stalled_count -= 1
                    stalled_count = max(stalled_count, 0)
                else:
                    self._print_thought("\n\nSTALLED:\n\n")
                    stalled_count += 1

                # Check if the evaluations are stagnant or decreasing over the last 3 checkpoints
                if len(self._checkpoints) >= 3:
                    # Retrieve the last three evaluations
                    last_evaluations = [cp["evaluation"] for cp in self._checkpoints[-3:]]
                    if all(e >= last_evaluations[i+1] for i, e in enumerate(last_evaluations[:-1])):
                    # If we've been stalled multiple times, see if we can just make an educated guess
                        times_stalled += 1
                        if times_stalled >= 2:
                            self._print_thought("Check if we can make an educated guess")
                            educated_guess_promt = f"""Given the following information

{facts}

Please answer the following question, including necessary reasoning:
    - Do you have two or more congruent pieces of information that will allow you to make an educated guess for the original request? The educated guess MUST answer the question.
Please output an answer in pure JSON format according to the following schema. The JSON object must be parsable as-is. DO NOT OUTPUT ANYTHING OTHER THAN JSON, AND DO NOT DEVIATE FROM THIS SCHEMA:

    {{
        "has_educated_guesses": {{
            "reason": string,
            "answer": boolean
        }}
    }}
""".strip()
                        self.orchestrated_messages.append({"role": "user", "content": educated_guess_promt, "name": sender.name})
                        response = self.client.create(
                            messages=self.orchestrated_messages,
                            cache=self.client_cache,
                            response_format={"type": "json_object"},
                        )
                        self.orchestrated_messages.pop()

                        extracted_response = self.client.extract_text_or_completion_object(response)[0]
                        data = None
                        try:
                            data = json.loads(extracted_response)
                        except json.decoder.JSONDecodeError as e:
                            # Something went wrong. Restart this loop.
                            self._print_thought(str(e))
                            break

                        self._print_thought(json.dumps(data, indent=4))
                        if data["has_educated_guesses"]["answer"]:
                            return True, "TERMINATE"

                        # Update facts first
                        self._print_thought("We aren't making progress. Evaluating options.")
                        new_facts_prompt = f"""It's clear we aren't making as much progress as we would like, but we may have learned something new. Please rewrite the following fact sheet, updating it to include anything new we have learned. This is also a good time to update educated guesses (please add or update at least one educated guess or hunch, and explain your reasoning). 

{facts}
""".strip()
                        self.orchestrated_messages.append({"role": "user", "content": new_facts_prompt, "name": sender.name})
                        response = self.client.create(
                            messages=self.orchestrated_messages,
                            cache=self.client_cache,
                        )
                        facts = self.client.extract_text_or_completion_object(response)[0]
                        self.orchestrated_messages.append({"role": "assistant", "content": facts, "name": self.name})

                        # Update plan next
                        historical_plans = [cp["plan"] for cp in self._checkpoints]
                        historical_evaluations = [cp["evaluation"] for cp in self._checkpoints]
                        historical_responses = [cp["response"] for cp in self._checkpoints]

                        # Create a new plan prompt reflecting the entire history of checkpoints and specific team considerations
                        new_plan_prompt = "We have noticed a lack of progress in our last three evaluations. Here's a comprehensive overview of all our efforts:\n"
                        for i in range(len(historical_plans)):
                            new_plan_prompt += f"Plan {i+1}: {historical_plans[i]}, Evaluation: {historical_evaluations[i]}, Insights: {historical_responses[i]}\n"
                        new_plan_prompt += "\nPlease develop a new plan expressed in bullet points, taking into account the following team composition and our entire history. We cannot include any outside individuals in this plan."
                        new_plan_prompt += f"\n\nTeam membership:\n{team}\n"
                        self.orchestrated_messages.append({
                            "role": "user",
                            "content": new_plan_prompt,
                            "name": sender.name
                        })

                        # Send the orchestrated messages to the client for processing
                        response = self.client.create(
                            messages=self.orchestrated_messages,
                            cache=self.client_cache,
                        )

                        plan = self.client.extract_text_or_completion_object(response)[0]
                        self._checkpoints = []
                        break

                # Broadcast the message to all agents
                m = {"role": "user", "content": data["instruction_or_question"]["answer"], "name": self.name}
                if m["content"] is None:
                    m["content"] = ""
                self._broadcast(m, out_loud=[data["next_speaker"]["answer"]])

                # Keep a copy
                m["role"] = "assistant"
                self.orchestrated_messages.append(m)

                # Request a reply
                for a in self._agents:
                    if a.name == data["next_speaker"]["answer"]:
                        reply = {"role": "user", "name": a.name, "content": a.generate_reply(sender=self)}
                        self.orchestrated_messages.append(reply)
                        a.send(reply, self, request_reply=False)
                        self._broadcast(reply, exclude=[a])
                        break

        return True, "TERMINATE"
