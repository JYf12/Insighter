"""
DeepAgents 人机协作：人工回应 + 恢复执行

演示人工在审批阶段直接提供回应（不执行工具），然后继续恢复执行
适用场景：Agent 需要澄清问题、获取人类判断或偏好时，人工直接回答

用户问题 -> Agent 请求澄清 -> 中断等待人工回应 -> Command(resume=...) 恢复执行
"""

import os

from deepagents import create_deep_agent
from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

load_dotenv(find_dotenv())

llm = init_chat_model(
    model=os.getenv("LLM_MODEL_ID"),
    model_provider="openai",
)


@tool
def deploy_to_environment(environment: str, service_name: str):
    """
    部署服务到指定环境

    这是高风险操作，需要人工确认部署目标
    """
    print(f"调用 deploy_to_environment 工具，准备部署 {service_name} 到 {environment}")
    return f"已将 {service_name} 部署到 {environment} 环境"


@tool
def send_notification(recipients: list, message: str):
    """
    发送通知消息

    需要人工确认收件内容和接收人
    """
    print(f"调用 send_notification 工具，发送给 {recipients}")
    return f"已发送通知给 {len(recipients)} 个接收人"


@tool
def request_human_guidance(question: str):
    """
    向人类寻求指导或澄清

    当 Agent 遇到无法自行决定的情况时，使用此工具暂停并等待人工回应

    参数:
        question: 需要人类回答的问题

    返回:
        人类的回应内容
    """
    print(f"\n[Agent 请求指导] {question}")

    # interrupt() 会暂停执行，等待人工通过 Command(resume=...) 提供回应
    human_response = interrupt({
        "type": "guidance_request",
        "question": question,
        "message": f"请回答以下问题：{question}"
    })

    # human_response 是人工通过 Command(resume={...}) 传入的值
    response_text = human_response.get("response", "未提供回应")
    print(f"[人类回应] {response_text}")

    return f"人类回应：{response_text}"


# 人机协作必须配置 checkpointer
checkpointer = InMemorySaver()

thread_config = {
    "configurable": {
        "thread_id": "hitl-respond-demo",
    }
}


main_agent = create_deep_agent(
    model=llm,
    tools=[deploy_to_environment, send_notification, request_human_guidance],
    checkpointer=checkpointer,
    system_prompt="""
    你是一个负责部署和通知的智能助手。
    
    重要规则：
    1. 在执行部署操作前，如果不确定部署到哪个环境，必须调用 request_human_guidance 询问人类
    2. 在发送通知前，如果不确定接收人或内容，也必须调用 request_human_guidance 确认
    3. 获得人类回应后，再执行相应的操作
    
    请使用中文回复执行结果。
    """,
    # 配置中断规则
    interrupt_on={
        "deploy_to_environment": True,      # 部署需要审批（approve/edit/reject/respond）
        "send_notification": True,           # 发送通知需要审批
        "request_human_guidance": True,      # 请求指导需要人工回应
    },
)


# ==================== 场景 1：Agent 主动请求人类指导 ====================
print("=" * 60)
print("场景 1：Agent 不确定部署环境，请求人类指导")
print("=" * 60)

result_1 = main_agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "帮我把 user-service 服务部署到生产环境，然后通知开发团队",
            }
        ]
    },
    config=thread_config,
)

# 检查是否有中断
interrupts = result_1.get("__interrupt__", [])

if interrupts:
    interrupt_value = interrupts[0].value
    action_requests = interrupt_value["action_requests"]

    print(f"\n本次中断的工具数量：{len(action_requests)}")
    for i, action in enumerate(action_requests, 1):
        print(f"  {i}. 工具名：{action['name']}")
        print(f"     参数：{action['args']}")

    decisions = []
    for action in action_requests:
        action_name = action["name"]

        if action_name == "request_human_guidance":
            # ✅ 使用 respond 类型：人工直接回答问题，不执行工具
            decisions.append({
                "type": "respond",
                "message": "先部署到 staging 环境进行测试，不要直接部署到 production"
            })
            print(f"\n[人工决策] 对 '{action_name}' 使用 respond 类型")
            print(f"[人工回应] 先部署到 staging 环境进行测试，不要直接部署到 production")

        elif action_name == "deploy_to_environment":
            # 修改部署目标为 staging
            decisions.append({
                "type": "edit",
                "edited_action": {
                    "name": action_name,
                    "args": {
                        "environment": "staging",  # 从 production 改为 staging
                        "service_name": action["args"]["service_name"]
                    }
                }
            })
            print(f"\n[人工决策] 对 '{action_name}' 使用 edit 类型，修改环境为 staging")

        elif action_name == "send_notification":
            # 批准发送通知
            decisions.append({"type": "approve"})
            print(f"\n[人工决策] 对 '{action_name}' 使用 approve 类型")

    # 恢复执行
    print("\n" + "=" * 60)
    print("恢复执行...")
    print("=" * 60)

    result_2 = main_agent.invoke(
        Command(resume={"decisions": decisions}),
        config=thread_config,
    )

    print(f"\n最终结果：{result_2['messages'][-1].content}")


# ==================== 场景 2：简化的 respond 用法 ====================
print("\n\n" + "=" * 60)
print("场景 2：直接使用 respond 提供信息（无工具执行）")
print("=" * 60)

# 重置 thread_id 用于新会话
thread_config_2 = {
    "configurable": {
        "thread_id": "hitl-respond-demo-2",
    }
}

result_3 = main_agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "我需要发送一条紧急通知，但不确定应该发给谁。请帮我确认接收人列表。",
            }
        ]
    },
    config=thread_config_2,
)

interrupts_2 = result_3.get("__interrupt__", [])

if interrupts_2:
    interrupt_value = interrupts_2[0].value
    action_requests = interrupt_value["action_requests"]

    decisions_2 = []
    for action in action_requests:
        if action["name"] == "request_human_guidance":
            # ✅ respond 的核心用法：人工直接提供答案
            decisions_2.append({
                "type": "respond",
                "message": "发送给所有后端开发人员：张三、李四、王五"
            })
        else:
            # 其他工具选择批准
            decisions_2.append({"type": "approve"})

    result_4 = main_agent.invoke(
        Command(resume={"decisions": decisions_2}),
        config=thread_config_2,
    )

    print(f"\n最终结果：{result_4['messages'][-1].content}")


print("\n\n" + "=" * 60)
print("respond 类型的关键要点")
print("=" * 60)
print("""
1. respond 用于人工直接提供信息，而不是执行工具
2. 常见场景：
   - Agent 请求澄清问题
   - 需要人类判断或偏好
   - 工具内部调用 interrupt() 等待回应
   
3. 使用格式：
   {
       "type": "respond",
       "message": "人工提供的回应内容"
   }
   
4. 与 approve/edit/reject 的区别：
   - approve/edit/reject：控制是否执行工具以及如何执行
   - respond：不执行工具，直接给人工回应作为工具的返回值
""")
