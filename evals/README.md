# 评估任务

一组固定的编码任务，用来比较不同模型、Provider 和配置下 MossCode 完成任务的能力和成本。`scripts/run_evals.py` 让 MossCode 在每个任务的项目副本里工作，结束后用它没见过的测试判定是否完成，记录通过率、请求次数、Token、缓存命中率、耗时和费用。

## 运行

```
python scripts/run_evals.py                                      # 默认 Provider，全部任务各一次
python scripts/run_evals.py --provider deepseek --provider kimi  # 对比两个 Provider
python scripts/run_evals.py --tasks "bugfix-*" --repeat 3        # 只跑修 bug 的任务，每个 3 次
python scripts/run_evals.py --output results.json --report report.md
python scripts/run_evals.py --list
```

配置的查找顺序与 `moss` 相同，`--config` 可以指定。每完成一次运行打印一行 JSON，全部完成后打印 Markdown 报告：先是每个 Provider 的汇总（通过数、通过率、平均请求次数、平均输入和输出 Token、对话缓存命中率、平均耗时、总费用、出错次数），再是每个任务在各 Provider 上的结果。输出中没有提示、回复或密钥。

会真实请求模型服务并产生费用：13 个任务跑一遍通常是几十到一百多次请求。

## 运行环境

- 每次运行把任务的 `repo/` 复制到系统临时目录中的新文件夹，MossCode 在那里工作，结束后删除；`--keep` 保留它，结果中的 `workdir` 指向它，便于查看 Agent 改了什么。
- 对话不带记忆、钩子、技能、MCP 和用户的全局指令（`~/.mosscode/AGENTS.md`），只用配置中的 Provider、系统提示、命令行解释器、`todo_write` 开关、最大迭代次数和压缩设置，这样不同机器、不同次的结果可以比较。和 `moss` 一样，临时目录上层文件夹中的 `AGENTS.md` 或 `CLAUDE.md` 仍会被读取。
- 权限模式默认 `bypassPermissions`：命令和编辑不经审批直接执行，Agent 可以自己跑测试。命令在临时目录中运行，但不在沙箱里，只在可信的机器上运行评估。`--permission-mode acceptEdits` 等模式下需要审批的操作会被拒绝，和 `-p` 一样。
- 每次运行的时限默认 900 秒（`--timeout`）；超时或服务出错时也会判定项目当前的状态。

## 判定

Agent 结束后，把任务的 `check/` 复制进项目（覆盖同名文件），用运行脚本的 Python 执行其中的 `test_*.py`，全部通过即为完成。`check/` 只在判定时出现，Agent 看不到；项目里写一个空的 `pytest.ini`，以免上层目录的 pytest 配置影响结果。

## 添加任务

每个任务是 `tasks/` 下的一个文件夹：

- `task.toml`：`category`（`bugfix`、`feature` 或 `refactor`）和 `prompt`（交给 Agent 的要求，写成用户会说的话，不提隐藏测试）；
- `repo/`：Agent 工作的项目，可以带一些可见的测试；
- `check/`：判定用的测试，文件名不能和 `repo/` 中的重复；
- `solution/`：参考修改，覆盖到 `repo/` 上即为完成的状态。

`tests/test_evals.py` 对每个任务检查：未修改的项目不能通过判定，加上参考修改后必须通过。任务只用标准库和 pytest，不联网。
