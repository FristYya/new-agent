import express from "express";

/**
 * New-Agent 课程脚手架。
 *
 * 目前只提供页面启动所需的只读占位接口；会逐步替换本文件并新增模块。
 */

const app = express();
const port = Number(process.env.PORT || 3100);

app.disable("x-powered-by");
app.use(express.json({ limit: "64kb" }));
app.use(express.static("public"));

// 完整前端带有登录界面。课程脚手架使用固定演示用户，避免认证业务干扰 Agent 主线。
app.get("/api/auth/me", (_request, response) => {
  response.json({
    user: {
      id: "course-user",
      username: "student",
      displayName: "课程学员",
    },
  });
});

// 让首页可以正常渲染。会逐步点亮模型、Tool、Skill、会话和运行状态。
app.get("/api/state", (_request, response) => {
  response.json({
    agents: [],
    skills: [],
    tools: [],
    knowledge: [],
    conversations: [],
    tasks: [],
    observability: { runs: [] },
    runtime: {
      model: "Pi SDK 尚未接入",
      busy: false,
      status: "starter",
    },
  });
});

// 前端启动时会拉取这两个工作台；脚手架返回空数据
app.get("/api/observability", (_request, response) => {
  response.json({ overview: {}, runs: [], alerts: [], skillStats: [], toolStats: [], trends: [] });
});

app.get("/api/evals", (_request, response) => {
  response.json({
    summary: { totalCases: 0 },
    cases: [],
    datasets: [],
    runs: [],
    jobs: [],
    gate: { minPassRate: 1, maxRegressions: 0 },
  });
});

// 课程尚未实现的写接口统一返回 501，方便在浏览器 Network 面板观察能力演进。
app.all(["/api/*", "/chat"], (_request, response) => {
  response.status(501).json({
    error: "脚手架尚未实现该 Agent 能力，请依次开发。",
  });
});

app.listen(port, () => {
  console.log(`New-Agent 脚手架：http://localhost:${port}`);
  console.log("完整前端已就绪；Agent、Tool、Skill、会话与评测能力等待实现。");
});
