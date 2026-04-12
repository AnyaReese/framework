### 总体架构

1. main.py
   - 解析命令行参数，初始化：
     - AndroidAppiumClient（设备与 Appium IO）
     - QuestionnaireState（问卷状态机）
     - GPTClient（LLM1/LLM2/LLM3 调用封装）
     - WorkflowRunner（核心流程）
   - 调用 WorkflowRunner.run(task)
2. workflow.py（核心）
   - 单线程主循环 + ThreadPoolExecutor 并发 LLM 调用
   - 维护：快照缓存、LLM 结果缓存、DFS 栈、StateGraph、probe 数据、beam 数据、replay/恢复逻辑
3. appium_android.py（设备侧）
   - capture_snapshot()：一次性抓取稳定 page_source + screenshot + deviceInfo（pixelRatio）
   - parse_xml_to_uist()：UiAutomator2 XML -> 内部 uist 树（含 bounds/属性/子树）
   - 低层动作：tap/back/click/type_text 等
4. ui_cls.py（UI 语义与稳定性）
   - BaseUI.post_process_ui()：根节点处理、去重、OCR、icon label、外部语义接口（可选）、分配 element_id（vid_map）
5. questionnaire_state.py（问卷状态）
   - 支持 v1(flat show_if) + v2(topic/gate)
   - open_gaps()、active_topics_top()、topic_tree_shallow()、build_topic_question_pack()
   - apply_updates()：粘性规则 + 证据存储 + 父子推断 + 可见性重算
6. gpt_cls.py（LLM schema 与调用）
   - LLM1：propose_navigation()
   - LLM2-1：propose_topic_routes()
   - LLM2-2：propose_topic_fill()
   - LLM3：recover_state()
   - 强制结构化输出
7. state_graph.py（可回放图）
   - 节点：state_sig
   - 边：action payload（支持多步 actions 列表）+ count + 验证统计
   - shortest_action_path()：BFS 找最短动作路径（边按 count 优先）
   - record_transition()：返回“dst 在发现时是否新状态”的布尔值（关键语义）

### 已实现：workflow（核心主流程）

1. 权威快照与状态签名（workflow.py 的 _capture_and_process）

- 采集：调用 appium.capture_snapshot()，得到 xml_raw + screenshot_raw（base64）+ device_info
- 归一化（为稳定 hash 与对齐坐标）：
  - 状态栏区域：对 screenshot 做“遮罩”(mask top)，对 xml 做“删除 top 区域节点”，同时保留 raw 版本用于调试
- UI 后处理：
  - uist = parse_xml_to_uist(xml_processed, pixel_ratio=coord_scale)
  - (uist2, vid_map) = BaseUI.post_process_ui(uist, screenshot_processed)
- 生成多粒度签名（都带前台包名/Activity，防止跨 App 冲突）：
  - state_sig：用于 LLM 缓存、graph key（包含少量 label，且会去掉数字，降低动态计数影响）
  - struct_sig：更稳定，用于“同结构页面”的 family 归并（sig_to_family[state_sig] = struct_sig）
  - coarse_sig / fine_sig：用于调试与辅助判断
- 快照缓存 key：xml_hash + screenshot_hash + coord_scale + postprocess bundle version + foreground_package/activity
  - 命中则复用 uist/vid_map，降低 OCR/外部语义成本

2. 主循环（WorkflowRunner.run）
   每一轮的顺序是“先把信息拉齐，再做动作”：

- drain futures：把已经完成的 NAV/TopicRoute/TopicFill 结果写入 cache，并立刻 apply_updates()
- 读取 open_gaps：如果 gaps 为空则停止（问卷完成）
- Foreground gate：检测当前前台包名是否在允许列表，不在则走 deterministic recovery/重启拉回
- Drift 检测：如果 UI 在“没有明确动作记录”的情况下变化，记录 drift 边并强制 replan
- NAV barrier：
  - 优先等待 LLM1（最多 nav_timeout_s），等待期间会周期性做 preflight 检查以发现 drift
  - 只有超时/冷却才允许启用 heuristics（避免“乱点”）
- overlay 处理：overlay_kind=dismiss/loading 优先处理（dismiss 走 LLM1 overlay_dismiss_actions；loading 走 bounded wait）
- probe-return：对候选动作做小规模试探（而后返回），收集“动作->响应”的 UI data
- forward commit：用 probe 结果选择下一步真正前进的动作并执行
- DFS 退栈/全局前沿/恢复/重启回放：在本地 exhausted 或 stuck 时触发

3. DFS 完整性（“可回溯探索”）

- workflow 维护 dfs_stack（当前路径栈）与 explored_actions（某页面 family 下已探索完的候选）
- 当前 state exhausted 时：
  - 先 backtrace 到最近的“还有未探索候选”的祖先
  - 没有祖先可回时，选择 global frontier 或触发 beam/restart+replay

4. overlay 与恢复（LLM3 + 确定性 ladder）

- 优先走确定性恢复（比如前台包不对、return 失败、overlay unresolved 等）
- 不行再调 LLM3 recover_state() 产出最多 5 步恢复动作
- 恢复动作同样会记录到 graph（用于后续 replay 可靠性）

### 已实现：open_gaps（问卷缺口定义与驱动）

1. open_gaps() 定义

- 只看“当前可见”的题（visible==True）
- 单选：answer is None => gap
- 多选：answer 为空 => gap

2. active_topics_top() 给 LLM1 的稳定上下文

- 以 topic_members 与 open_gaps 的交集计数排序，输出 top-K topic：
  {topic_id, title, open_gaps, keywords}
- 目的：不给 LLM1 喂全量题目，减少 token 与波动

3. topic_tree_shallow() 给 LLM2-1 的 routing

- 输出每个 topic 的轻量信息：title、keywords、少量 example_questions

### 已实现：问卷填写（LLM2，现为 Topic Router + Topic Filler）

重要说明：gpt_cls.py 里存在 propose_questionnaire_updates()（按全量 gaps 做更新），但 workflow.py 当前没有调用它；实际跑的是 LLM2-1/LLM2-2 两段式。

1. LLM2-1（Topic Router）

- 触发条件：open_gaps 非空且该 state_sig 还没有 route cache / future
- 输入：
  - topic_tree_shallow（稳定 topic 列表）
  - page_signals（从当前 uist 抽取的 OCR/文本 top lines + nav page_tags/ui_type/page_summary）
- 输出：relevant_topics（topic_id + confidence + rationale + expected_evidence）

2. LLM2-2（Topic Filler）

- 触发条件：
  - relevant_topics 中 conf >= topic_route_conf_threshold
  - 该 topic 在当前问卷里仍有 gaps（open_gaps_in_topic(topic_id) 非空）
  - (sig, topic_id) 未在 cooldown 内，且 answers_digest_for_topic 变化后才重新调
- 输入：
  - question_pack（只含该 topic 的一小包题，limit=topic_pack_limit）
  - memory（topic_memory_summary，高置信/关键 gate 的历史摘要）
  - current_answers（用于避免重复写值）
  - 当前屏幕 ui_digest + screenshot（必须以当前 UI 证据为准）
- 输出：QuestionnaireUpdate.proposed_updates（最多 24 条）
- 应用：apply_updates() 立即更新（粘性 gate、单选置信升级、多选并集、child->parent 推断、可见性重算）

3. 要点（apply_updates 里）

- gate（Yes/No）一旦 Yes，不允许低置信度改回 No（需要 conf>=0.95 才允许降级）
- 多选默认做并集，不做删除（除非后续允许扩展“明确矛盾且超高置信”的删除路径）
- evidence_refs 与 evidence_summary 会累积写入，便于演示解释“为什么这样填”

### 已实现：NAV（LLM1）+ probe-return

1. LLM1（propose_navigation）

- 输入：当前 screenshot + ui_digest（稳定 element_id）+ active_topics_top + history + state_sig
- 输出（关键字段）：
  - overlay_kind + overlay_dismiss_actions
  - candidate_actions：每个候选是 ActionCandidate(actions[1..3], score[-1..1], tags, return_method/return_actions 可选)
  - 全局 return_method/return_actions 作为默认兜底

2. NAV barrier（不让“没 LLM 就乱点”）

- 优先等 NAV ready（nav_timeout_s）
- 等待期间做 drift check（避免拿旧 plan）
- 超时则 cancel future 并进入 heuristics 模式（只在这时允许）

3. probe-return（用“试探”换“可验证的 forward 决策”）

- 每轮最多 probe per_page_probe_cap 个候选
- probe 产出：
  - probe_outcomes\[family(src)][cand_key] = dst_sig
  - probe_novelty\[family(src)][cand_key] = dst_was_new_at_discovery（从 StateGraph.record_transition 的返回值拿）
- return 策略（严格顺序）：
  1. cand.return_actions（若有）优先执行
  2. return_method=tab-back/custom 时，尝试 heuristic tab elements
  3. return_method=close/custom 时，尝试 heuristic close elements
  4. BACK 作为最后兜底（tab-back/custom 下更晚才按 BACK）
- element_id 不存在处理：
  - return_actions 的每个 click，会从“源快照 vid_map”生成 fingerprint
  - 执行时若当前 vid_map 的同 id 不可信，则按 fingerprint 在当前页面找最匹配的元素 remap，再点

### 已实现：beam-search

beam-search 在 workflow.py 的核心入口是 _maybe_beam_switch(cur_sig, local_score, cur_snap, task)。

1. 目标：当“本地 forward 的可验证收益”明显不如“跳到某个已知 state 的收益/成本比”时，允许全局切换。
2. 候选集合（beam）

- 从 StateGraph.nodes 遍历所有 sig，过滤：
  - overlay 节点（dismiss/loading）
  - exhausted 节点（该页面 family 的候选都 explored 了）
  - 在 dfs_stack 内的节点（祖先由 backtrace 处理，避免重启来回跳）
- 先按 Value(sig) 排序，取 beam_width 个进入成本评估

3. 评分公式（对应代码中的 value/cost/score）
   对任意目标状态 t：

(1) Value(t) 定义
Value(t) = Novelty(t) + VisitScore(t) + YieldScore(t) + TopicScore(t) + UiTypeBonus(t)

其中：

- visits(t) = graph.nodes[t].visit_count（无则按 1）
- Novelty(t) =
  - 8.0, 如果 visits(t) <= 1
  - 2.0, 否则
- VisitScore(t) = 3.0 / max(1, visits(t))
- YieldScore(t) = min(8.0, 1.2 * state_update_counts[t])
  - state_update_counts[t]：该 state 上 topic_fill 产生的 proposed_updates 条数累计
- TopicScore(t)：
  - 若 nav_cache[t] 有 page_tags：tag 命中当前 active_topics 的关键词 blob，则加 4.0 * clamp(weight, 0.2..1.0)
  - 若 topic_route_cache[t] 有 relevant_topics：取其中“仍有 gaps 的 topic”的最大 confidence，贡献 6.0 * confidence
  - 取两者的 max，并上限 10.0
- UiTypeBonus(t)：
  - 2.5, 若 nav.ui_type 属于 {settings_list, detail_form, permissions_dialog, subscription_paywall, auth_flow}
  - 0.0, 其它

(2) Cost(t) 定义
Cost(t) = TravelCost(t) + RestartPenalty(t) + EdgePenalty(t)

- TravelCost(t) = travel_cost_weight * steps(t)
- steps(t) 的计算：
  - 优先尝试 in-graph：steps = len(shortest_action_path(cur_sig, t))
  - 若不可达，则尝试 restart+replay：用 replay_src = restart_entry_sig 或 entry_sig 或 cur_sig，再计算 steps = len(shortest_action_path(replay_src, t))
  - 仍不可达则该 t 丢弃
- RestartPenalty(t)：
  - 0.0, 如果 reach_mode=in_graph
  - restart_penalty_dynamic(now), 如果 reach_mode=restart_replay
  - restart_penalty_dynamic 会随着“近期重启次数接近/超过预算”增大（避免反复重启跳来跳去）
- EdgePenalty(t)：
  EdgePenalty(t) = edge_unverified_penalty * unverified_edges(t) + edge_stale_penalty * stale_edges(t)
  - unverified_edges：路径上 edge.last_verified_ts==0 的边数量
  - stale_edges：已验证但距离现在超过 edge_stale_after_s 的边数量
  - 这些统计来自 replay_distance_cache（由 _estimate_replay_steps 填充）

(3) 最终分数
Score(t) = Value(t) - Cost(t)

1. 切换条件（必须同时满足）

- cooldown：距离上次 jump 小于 jump_cooldown_s 则不切
- commit 机制：一旦决定目标，会保持一段时间/动作数（beam_commit_min_s + beam_commit_min_actions），除非新目标优势超过 beam_commit_override_gain
- gain 门槛：Score(target) >= local_score + min_switch_gain 才允许从本地切走

2. 执行方式

- reach_mode=in_graph：调用 _navigate_via_graph（按 graph 已知边走；发生偏离即停止并 fallback）
- reach_mode=restart_replay：调用 _restart_and_replay（重启后按 graph 路径回放）
- 回放动作执行支持 fingerprint remap（与 return_action 同类机制），并对边写入 verified_ok/verified_fail

### 未实现

1. 仅预留了 vision-semantic supplement 的接口
   现状：

- ui_cls.py 已实现 ExternalSemanticConfig + _attach_external_semantic()，并在 post_process_ui() 里调用
- 默认是关闭状态：只有设置环境变量 UI_SEMANTIC_ENDPOINT 才会真正发请求
  缺口：
- 外部服务本身（接口、稳定性、输出格式、效果评估）不在 repo 内；演示时应说明“代码已留接口与合并逻辑，但没有默认可用的后端服务/数据”

2. 动态内容（Is UI changed？hash key）的适配
   现状（基础能力已有）：

- preflight：用 processed xml_hash 或 screenshot_hash 判断是否变化，变了才进行 refresh
- state_sig：去掉数字、量化坐标，降低动态计数抖动；同时维护 struct_sig，并把 state_sig 映射到 family（sig_to_family）
- 探索/统计的多处 key 都用 family_id（例如 attempted/explored/probe_outcomes），避免“同结构不同 state_sig”绕过去重
  缺口（“完整动态适配”还没做的点）：
- graph 节点与 LLM cache 仍以 state_sig 为主键，没有做 family-level 合并（会出现同结构页面碎片化、多次重复 NAV）
- 没有做“动态区域屏蔽/分区 hash”（例如列表滚动区域单独处理），因此 倒计时/feed/列表类页面仍可能频繁变更触发 refresh 与 cache miss

3. Replay 时动态内容路径的处理
   现状（基础能力已有）：

- graph 边存储 actions 列表；每步可带 fingerprint；回放时会在当前 vid_map 里做 remap（找最匹配元素）
- 回放过程中有 divergence check（允许 family 匹配），并记录边的验证统计用于后续 beam 的边惩罚
  缺口（“动态路径”完整闭环未做）：
- 对“滚动列表/元素重排/分页加载”的可回放策略不足：当前主要依赖 resource_id_tail + label + bbox 的 fingerprint，遇到列表项位置变化/复用 cell 时仍容易失效
- 没有内置“滚动定位到某个 item”的动作类型与定位算法（通常需要额外的 selector/文本搜索/视觉匹配策略）

4. 探索策略可能可以再结合用户评论/其他meta信息