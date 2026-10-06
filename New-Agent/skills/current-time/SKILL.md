---
# Skill 的唯一名称
name: current-time

# 帮助模型判断何时该启用本skill
description: 查询当前服务器的日期、时间、时区、时间戳和 ISO 格式时间。当用户询问现在几点、今天的日期、当前时间戳或指定时区的时间时，使用此 Skill。

# 限制本 Skill 只能请求这个 Tool
allowed-tools: current_time
---


# 当前时间

仅当用户询问当前时间、日期、时间戳或时区信息时，才使用此 Skill

# 执行流程
1、调用 `current_time` Tool。
2、根据用户的问题，返回本地格式化时间、ISO 格式时间、日期、时区和时间戳
3、如果此 Skill 或者对应的 Tool 已被禁用，应该明确告诉用户当前能力不可用，不要尝试用使用其他方法绕过此限制