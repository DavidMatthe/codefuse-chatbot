from pydantic import BaseModel
from typing import List, Union
import re
import json
import traceback
import copy
from loguru import logger

from langchain.prompts.chat import ChatPromptTemplate

from dev_opsgpt.connector.schema import (
    Memory, Task, Env, Role, Message, ActionStatus
)
from dev_opsgpt.llm_models import getChatModel
from dev_opsgpt.connector.configs.agent_config import REACT_PROMPT_INPUT

from .base_agent import BaseAgent


class ReactAgent(BaseAgent):
    def __init__(
            self, 
            role: Role,
            task: Task = None,
            memory: Memory = None,
            chat_turn: int = 1,
            do_search: bool = False,
            do_doc_retrieval: bool = False,
            do_tool_retrieval: bool = False,
            temperature: float = 0.2,
            stop: Union[List[str], str] = None,
            do_filter: bool = True,
            do_use_self_memory: bool = True,
            focus_agents: List[str] = [],
            focus_message_keys: List[str] = [],
            # prompt_mamnger: PromptManager
            ):
        
        super().__init__(role, task, memory, chat_turn, do_search, do_doc_retrieval, 
                         do_tool_retrieval, temperature, stop, do_filter,do_use_self_memory,
                         focus_agents, focus_message_keys
                         )

    def run(self, query: Message, history: Memory = None, background: Memory = None, memory_pool: Memory = None) -> Message:
        '''agent reponse from multi-message'''
        for message in self.arun(query, history, background, memory_pool):
            pass
        return message

    def arun(self, query: Message, history: Memory = None, background: Memory = None, memory_pool: Memory = None) -> Message:
        '''agent reponse from multi-message'''
        step_nums = copy.deepcopy(self.chat_turn)
        react_memory = Memory(messages=[])
        # insert query
        output_message = Message(
                role_name=self.role.role_name,
                role_type="ai", #self.role.role_type,
                role_content=query.input_query,
                step_content="",
                input_query=query.input_query,
                tools=query.tools,
                parsed_output_list=[query.parsed_output],
                customed_kargs=query.customed_kargs
                )
        query_c = copy.deepcopy(query)
        query_c = self.start_action_step(query_c)
        if query.parsed_output:
            query_c.parsed_output = {"Question": "\n".join([f"{v}" for k, v in query.parsed_output.items() if k not in ["Action Status"]])}
        else:
            query_c.parsed_output = {"Question": query.input_query}
        react_memory.append(query_c)
        self_memory = self.memory if self.do_use_self_memory else None
        idx = 0
        # start to react
        while step_nums > 0:
            output_message.role_content = output_message.step_content
            prompt = self.create_prompt(query, self_memory, history, background, react_memory, memory_pool)
            try:
                content = self.llm.predict(prompt)
            except Exception as e:
                logger.warning(f"error prompt: {prompt}")
                raise Exception(traceback.format_exc())
            
            output_message.role_content = "\n"+content
            output_message.step_content += "\n"+output_message.role_content
            yield output_message

            # logger.debug(f"{self.role.role_name}, {idx} iteration prompt: {prompt}")
            logger.info(f"{self.role.role_name}, {idx} iteration step_run: {output_message.role_content}")

            output_message = self.message_utils.parser(output_message)
            # when get finished signal can stop early
            if output_message.action_status == ActionStatus.FINISHED or output_message.action_status == ActionStatus.STOPED: break
            # according the output to choose one action for code_content or tool_content
            output_message, observation_message = self.message_utils.step_router(output_message)
            output_message.parsed_output_list.append(output_message.parsed_output)
            
            react_message = copy.deepcopy(output_message)
            react_memory.append(react_message)
            if observation_message:
                react_memory.append(observation_message)
                output_message.parsed_output_list.append(observation_message.parsed_output)
                # logger.debug(f"{observation_message.role_name} content: {observation_message.role_content}")
            # logger.info(f"{self.role.role_name} currenct question: {output_message.input_query}\nllm_react_run: {output_message.role_content}")

            idx += 1
            step_nums -= 1
            yield output_message
        # react' self_memory saved at last
        self.append_history(output_message)
        # update memory pool
        # memory_pool.append(output_message)
        output_message.input_query = query.input_query
        # end_action_step
        output_message = self.end_action_step(output_message)
        # update memory pool
        memory_pool.append(output_message)
        yield output_message
    
    def create_prompt(
            self, query: Message, memory: Memory =None, history: Memory = None, background: Memory = None, react_memory: Memory = None, memory_pool: Memory= None, 
            prompt_mamnger=None) -> str:
        '''
        role\task\tools\docs\memory
        '''
        # 
        doc_infos = self.create_doc_prompt(query)
        code_infos = self.create_codedoc_prompt(query)
        # 
        formatted_tools, tool_names, _ = self.create_tools_prompt(query)
        task_prompt = self.create_task_prompt(query)
        background_prompt = self.create_background_prompt(background)
        history_prompt = self.create_history_prompt(history)
        selfmemory_prompt = self.create_selfmemory_prompt(memory, control_key="step_content")
        # 
        # extra_system_prompt = self.role.role_prompt
        prompt = self.role.role_prompt.format(**{"formatted_tools": formatted_tools, "tool_names": tool_names})

        # react 流程是自身迭代过程，另外二次触发的是需要作为历史对话信息
        # input_query = react_memory.to_tuple_messages(content_key="step_content")
        # # input_query = query.input_query + "\n" + "\n".join([f"{v}" for k, v in input_query if v])
        # input_query = "\n".join([f"{v}" for k, v in input_query if v])
        input_query = "\n".join(["\n".join([f"**{k}:**\n{v}" for k,v in _dict.items()]) for _dict in react_memory.get_parserd_output()])
        # logger.debug(f"input_query: {input_query}")
        
        prompt += "\n" + REACT_PROMPT_INPUT.format(**{"query": input_query})

        task = query.task or self.task
        # if task_prompt is not None:
        #     prompt += "\n" + task.task_prompt

        # if doc_infos is not None and doc_infos!="" and doc_infos!="不存在知识库辅助信息":
        #     prompt += f"\n知识库信息: {doc_infos}"

        # if code_infos is not None and code_infos!="" and code_infos!="不存在代码库辅助信息":
        #     prompt += f"\n代码库信息: {code_infos}"

        # if background_prompt:
        #     prompt += "\n" + background_prompt

        # if history_prompt:
        #     prompt += "\n" + history_prompt

        # if selfmemory_prompt:
        #     prompt += "\n" + selfmemory_prompt

        # logger.debug(f"{self.role.role_name}  extra_system_prompt: {self.role.role_prompt}")
        # logger.debug(f"{self.role.role_name}  input_query: {input_query}")
        # logger.debug(f"{self.role.role_name}  doc_infos: {doc_infos}")
        # logger.debug(f"{self.role.role_name}  tool_names: {tool_names}")
        # prompt += "\n" + REACT_PROMPT_INPUT.format(**{"query": input_query})

        # prompt = extra_system_prompt.format(**{"query": input_query, "doc_infos": doc_infos, "formatted_tools": formatted_tools, "tool_names": tool_names})
        while "{{" in prompt or "}}" in prompt:
            prompt = prompt.replace("{{", "{")
            prompt = prompt.replace("}}", "}")
        return prompt
    
