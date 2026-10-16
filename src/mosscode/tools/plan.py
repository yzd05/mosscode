"""The plan-mode exit: the permission policy asks the user and switches modes."""

from pydantic import Field

from .base import Params, Tool


class PlanParams(Params):
    plan: str = Field(min_length=1, max_length=20000)


class ExitPlanMode(Tool):
    name = "exit_plan_mode"
    plan_only = True  # Not read-only: runs alone, after any reads in the same response.
    description = ("计划模式下调查完成后调用，提交 Markdown 格式的实施计划请用户审阅。用户批准后立即退出计划模式，"
                   "可以继续使用写入、编辑和命令工具执行计划；用户选择继续规划时仍处于计划模式。")
    prompt_summary = "提交计划，请用户批准后开始执行"
    prompt_guidelines = (
        "计划模式下完成调查后用 exit_plan_mode 提交计划，不要用文字询问用户是否开始执行；计划写明要改的文件、步骤和验证方法，简洁具体。",
        "只为需要动手实施的任务提交计划；纯调查或问答直接回答。用户选择继续规划时，根据反馈调整后再提交。",
    )
    params_model = PlanParams

    async def execute(self, params: PlanParams):
        return ("用户已批准计划，已退出计划模式。现在按计划执行；写入、编辑和命令按当前权限档位处理。",
                {"plan": params.plan})
