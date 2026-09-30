from agent_v2 import AuditMessage, UserMessage, convert_to_llm

internal_message = [
    UserMessage("读取 README"),
    AuditMessage("用户 amu 在测试"),
]

print("内部消息的数量：", len(internal_message))
print("发给模型的消息：", convert_to_llm(internal_message))