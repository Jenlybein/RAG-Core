# Issue tracker：GitHub

本仓库的 issue 与 spec 都以 GitHub issue 形式存放。所有操作使用 `gh` CLI。

## 约定

- **创建 issue**：`gh issue create --title "..." --body "..."`。多行正文用 heredoc。
- **读取 issue**：`gh issue view <number> --json number,title,body,labels,comments`。
- **列出 issue**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，配合合适的 `--label` 与 `--state` 过滤。
- **把 issue 设为某 issue 的子 issue**：`gh issue create --parent <parent> ...`，或事后 `gh issue edit <parent> --add-sub-issue <child>`（`gh` 2.94+）。旧版 `gh`：`gh api --method POST repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`（数据库 id，见下方 **Blocking**）。子 issue 不可用时，在子 issue 正文顶部写 `Part of #<parent>`。
- **Blocking（阻塞关系）**：GitHub 的**原生 issue 依赖**，是规范且 UI 可见的表示。用 `gh issue create --blocked-by <n>,<n> ...` 或 `gh issue edit <child> --add-blocked-by <blocker>` 添加边（`gh` 2.94+）。旧版 `gh`：`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，其中 `<blocker-db-id>` 是阻塞方的数字**数据库 id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，**不是** `#number` 或 `node_id`）。依赖功能不可用时，退回在子 issue 正文顶部写 `Blocked by: #<n>, #<n>`。所有阻塞方都关闭后，该 ticket 即解除阻塞。
- **统计未关闭的阻塞方**：`gh api repos/<owner>/<repo>/issues/<n> --jq .issue_dependencies_summary.blocked_by`（仅未关闭；为 0 即可以开始）。`gh issue view <n> --json blockedBy` 也会列出已关闭的阻塞方，需过滤：`--jq '[.blockedBy.nodes[] | select(.state == "OPEN")] | length'`。
- **评论 issue**：`gh issue comment <number> --body "..."`
- **添加 / 移除标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：先用 `gh issue comment <number> --body "..."` 发布说明，再 `gh issue close <number>`。对已关闭的 issue 执行 `gh issue close --comment` 会丢掉评论但仍以 0 退出。

从 `git remote -v` 推断仓库；在 clone 内运行时 `gh` 会自动推断。

只向用户自己的仓库提交 issue。除非用户拥有 `mattpocock/skills`（这些 skill 的来源仓库），否则绝不向其提交。在 fork 中 `gh` 可能默认指向 upstream 父仓库，因此每次会话首次 `gh issue create` 之前，用 `gh repo view --json nameWithOwner` 确认目标；若并非用户自己的仓库，先向用户确认。

## 把 PR 作为 triage 入口

**PRs as a request surface（PR 作为需求入口）：否。** _（若本仓库把外部 PR 当作功能需求处理，改为 `yes`；`/triage` 会读这个开关。）_

设为 `yes` 时，PR 与 issue 走同一套标签与状态，使用对应的 `gh pr` 命令：

- **读取 PR**：`gh pr view <number>`，然后 `gh pr view <number> --comments`（非 TTY 下 `--comments` 只打印评论，不打印正文），diff 用 `gh pr diff <number>`。
- **列出待 triage 的外部 PR**：`gh api --paginate 'repos/{owner}/{repo}/pulls?state=open' --jq '.[] | select(.author_association | IN("OWNER","MEMBER","COLLABORATOR") | not) | {number, title, author: .user.login, author_association, labels: [.labels[].name]}'`。
- **评论 / 打标签 / 关闭**：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`。

GitHub 的 issue 与 PR 共用同一编号空间，所以裸 `#42` 可能是其中之一：先用 `gh pr view 42` 解析，回退到 `gh issue view 42`。

## 当某个 skill 说“发布到 issue tracker”

创建一个 GitHub issue。

## 当某个 skill 说“取出相关 ticket”

按上文 **读取 issue** 的方式读取。

## Wayfinding 操作

供 `/wayfinder` 使用。**map** 是一个 issue，**child** issue 是它的 ticket。

- **Map**：单个带 `wayfinder:map` 标签的 issue，承载 Notes / Decisions-so-far / Fog 正文。`gh issue create --label wayfinder:map`。
- **子 ticket**：一个作为 map 子 issue 关联到 map 的 issue（见 **把 issue 设为某 issue 的子 issue**）。子 issue 不可用时，把子 ticket 加入 map 正文的任务列表，并在子 issue 正文顶部写 `Part of #<map>`。标签：`wayfinder:<type>`（`research`/`prototype`/`grilling`/`task`）。被认领后，指派给负责的开发者。
- **Blocking**：见「约定」下的 **Blocking**。
- **Frontier 查询**：列出 map 未关闭的子 issue（`gh issue list --state open`，范围限定为 map 的子 issue / 任务列表），剔除存在未关闭阻塞方的（见 **统计未关闭的阻塞方**，或 `Blocked by` 行中未关闭的 issue）以及已有指派的；按 map 顺序第一个胜出。
- **认领**：`gh issue edit <n> --add-assignee @me`，本次会话的第一次写操作。
- **解决**：`gh issue comment <n> --body "<answer>"`，然后 `gh issue close <n>`，最后把上下文指针（gist + 链接）追加到 map 的 Decisions-so-far。
