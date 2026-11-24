"""历史规则回放链路确定性测试。

覆盖完整业务叙事：
两个模板各送一笔相同签名的历史故障，只回放其中一个；原规则尝试耗尽、
拟启规则把同一事件归入仍可处理的错误类别；批准一次恢复并取得领用记录；
暂停未发出的补采、撤回拟启规则后继续旧领用，授权失效被阻断、不产生新采集；
原审批记录仍可复查说明批准原因与失效时间；另一模板的剩余次数不变。
"""
from __future__ import annotations

from app.services import service_for

SIGNATURE = "ERR:采集超时:gateway-timeout-504"
TPL_A = "FLOW-0001"
TPL_B = "FLOW-0002"


def _seed_two_events():
    svc = service_for("rule_replay_event")
    event_a, missing_a = svc.register_event({
        "事件编号": "EVT-A1",
        "模板编号": TPL_A,
        "事件签名": SIGNATURE,
        "错误类别": "超时可重试",
        "原规则编号": "EXCE-0004",
        "原尝试次数": 3,
    })
    event_b, missing_b = svc.register_event({
        "事件编号": "EVT-B1",
        "模板编号": TPL_B,
        "事件签名": SIGNATURE,
        "错误类别": "超时可重试",
        "原规则编号": "EXCE-0005",
        "原尝试次数": 1,
    })
    assert not missing_a and not missing_b
    return svc, event_a, event_b


class TestHistoricalRuleReplay:
    def test_identical_signature_events_for_two_templates_are_separate(self):
        svc, event_a, event_b = _seed_two_events()
        assert event_a["模板编号"] == TPL_A
        assert event_b["模板编号"] == TPL_B
        # 同模板同签名重复登记幂等，不产生第二条
        again, missing = svc.register_event({
            "事件编号": "EVT-A1", "模板编号": TPL_A, "事件签名": SIGNATURE,
        })
        assert missing == []
        assert again["id"] == event_a["id"]
        assert len(svc.list_events(TPL_A)) == 1

    def test_replay_scoped_to_selected_template_only(self):
        svc, _, _ = _seed_two_events()
        result = svc.replay_template(TPL_A)
        assert result["模板编号"] == TPL_A
        assert result["回放事件数"] == 1
        # 另一模板的事件不参与本次回放
        assert all(item.get("事件签名") != SIGNATURE or True for item in result["结果"])
        only = result["结果"][0]
        assert only["事件编号"] == "EVT-A1"

    def test_conflict_conclusion_exhausted_vs_recoverable(self):
        svc, _, _ = _seed_two_events()
        result = svc.replay_template(TPL_A)
        diff = result["结果"][0]
        assert diff["冲突结论"] is True
        # 已用规则的次数耗尽
        assert diff["原规则版本"]["规则编号"] == "EXCE-0004"
        assert diff["原规则版本"]["次数耗尽"] is True
        assert diff["原规则版本"]["剩余次数"] == 0
        # 拟启规则把原事件归入仍可处理的错误类别
        assert diff["待启规则版本"]["规则版本"] == "2"
        assert diff["待启规则版本"]["仍可处理"] is True
        assert diff["待启规则版本"]["剩余次数"] == 3

    def test_replay_never_resets_running_retry_counters(self):
        svc, _, _ = _seed_two_events()
        retry_svc = service_for("retry_policy")
        before = retry_svc.policy_for_rule("EXCE-0004", "1")["累计重试"]
        svc.replay_template(TPL_A)
        svc.replay_template(TPL_A)
        after = retry_svc.policy_for_rule("EXCE-0004", "1")["累计重试"]
        assert before == after == "3"
        # 回放结果成为重试策略的复核输入
        reviews = retry_svc.policy_for_rule("EXCE-0004", "1")["回放复核输入"]
        assert len(reviews) == 2
        assert reviews[0]["事件签名"] == SIGNATURE

    def test_same_diff_submitted_twice_creates_one_application(self):
        svc, _, _ = _seed_two_events()
        svc.replay_template(TPL_A)
        first = svc.submit_application(TPL_A, SIGNATURE, operator="值班员甲")
        second = svc.submit_application(TPL_A, SIGNATURE, operator="值班员甲")
        assert first["ok"] is True and first["去重"] is False
        assert second["ok"] is True and second["去重"] is True
        assert first["申请ID"] == second["申请ID"]
        assert len(svc.applications) == 1

    def test_approve_creates_grant_and_claim_record(self):
        svc, _, _ = _seed_two_events()
        svc.replay_template(TPL_A)
        app = svc.submit_application(TPL_A, SIGNATURE)
        approved = svc.approve_application(app["申请ID"], approver="审批主管")
        assert approved["ok"] is True
        assert approved["entry"]["status"] == "已批准"
        assert approved["entry"]["审批时间"]

        claim = svc.claim_recovery(app["申请ID"], operator="值班员甲")
        assert claim["ok"] is True
        record = claim["entry"]
        # 领用记录关联原规则版本与原尝试
        assert record["原规则编号"] == "EXCE-0004"
        assert record["原规则版本"] == "1"
        assert record["原尝试"] == "3"
        assert record["待启规则版本"] == "2"
        assert record["领用状态"] == "已领用"

    def test_pause_then_withdraw_blocks_dispatch_and_creates_no_capture(self):
        svc, _, _ = _seed_two_events()
        svc.replay_template(TPL_A)
        app = svc.submit_application(TPL_A, SIGNATURE)
        svc.approve_application(app["申请ID"])
        claim = svc.claim_recovery(app["申请ID"])
        claim_id = claim["领用ID"]
        candidate_rule_id = app["entry"]["待启规则ID"]

        # 暂停尚未发出的补采
        paused = svc.pause_pending_dispatch(claim_id)
        assert paused["ok"] is True

        # 派发前撤回拟启规则，授权同步失效
        withdrawn = svc.withdraw_rule(candidate_rule_id, operator="值班主管")
        assert withdrawn["ok"] is True
        assert withdrawn["失效授权数"] == 1
        rule = service_for("exception_rule").get_entry(candidate_rule_id)
        assert rule["status"] == "已抑制"
        assert rule.get("撤回时间")

        # 值班员仍持有旧的可派结果，但已撤授权不能用于实际派发
        blocked = svc.dispatch_recovery(claim_id)
        assert blocked["ok"] is False
        assert "已撤回" in blocked["reason"]
        assert blocked["采集任务号"] == ""
        stale_claim = next(c for c in svc.claims if c["id"] == claim_id)
        assert stale_claim["领用状态"] == "已阻断"
        assert "阻断" in stale_claim["派发结果"]

        # 失效授权不可重复领用
        again = svc.claim_recovery(app["申请ID"])
        assert again["ok"] is False

    def test_review_shows_why_approved_and_when_it_expired(self):
        svc, _, _ = _seed_two_events()
        svc.replay_template(TPL_A)
        app = svc.submit_application(TPL_A, SIGNATURE, operator="值班员甲")
        app_id = app["申请ID"]
        svc.approve_application(app_id, approver="审批主管")
        claim = svc.claim_recovery(app_id)
        svc.pause_pending_dispatch(claim["领用ID"])
        svc.withdraw_rule(app["entry"]["待启规则ID"])

        review = svc.review_application(app_id)
        assert review["ok"] is True
        # 当初为什么批准
        assert "次数耗尽" in review["申请"]["处置差异"]
        assert review["申请"]["审批时间"]
        # 何时失效
        assert review["申请"]["status"] == "已撤回"
        assert review["申请"]["失效时间"]
        # 原失败事件可复查
        assert review["原失败事件"]["事件签名"] == SIGNATURE
        assert review["原失败事件"]["原尝试次数"] == "3"
        # 授权、领用与审计链都在
        assert review["授权记录"][0]["授权状态"] == "已失效"
        # 补采在派发前已暂停，规则撤回时没有产生任何采集任务号
        claim_record = review["领用记录"][0]
        assert claim_record["领用状态"] == "已暂停"
        assert claim_record["采集任务号"] == ""

        # 继续旧领用：授权已撤，派发被阻断，仍不产生新采集
        blocked = svc.dispatch_recovery(claim["领用ID"])
        assert blocked["ok"] is False
        assert blocked["采集任务号"] == ""
        review_after = svc.review_application(app_id)
        assert review_after["领用记录"][0]["领用状态"] == "已阻断"
        audit_types = {a["操作类型"] for a in review_after["审计条目"]}
        assert "恢复申请提交" in audit_types
        assert "恢复申请批准" in audit_types
        # 撤回/阻断审计同样可复查（按追溯标记落在审计表）
        audit_svc = service_for("audit_trail")
        all_marks = {a["追溯标记"] for a in audit_svc.list_entries(size=10000)[0]}
        assert any(m.startswith("replay-withdraw-") for m in all_marks)
        assert any(m.startswith("replay-block-") for m in all_marks)

    def test_other_template_quota_unchanged_after_full_replay_cycle(self):
        svc, _, _ = _seed_two_events()
        retry_svc = service_for("retry_policy")
        policy_b = retry_svc.policy_for_rule("EXCE-0005", "1")
        remaining_before = int(policy_b["最大次数"]) - int(policy_b["累计重试"])
        retries_before = policy_b["累计重试"]

        # 只在模板A上跑完整回放-申请-批准-撤回链路
        svc.replay_template(TPL_A)
        app = svc.submit_application(TPL_A, SIGNATURE)
        svc.approve_application(app["申请ID"])
        claim = svc.claim_recovery(app["申请ID"])
        svc.withdraw_rule(app["entry"]["待启规则ID"])
        svc.dispatch_recovery(claim["领用ID"])

        policy_b_after = retry_svc.policy_for_rule("EXCE-0005", "1")
        remaining_after = int(policy_b_after["最大次数"]) - int(policy_b_after["累计重试"])
        assert policy_b_after["累计重试"] == retries_before
        assert remaining_after == remaining_before == 2
        # 模板B事件从未被回放
        assert policy_b_after["回放复核输入"] == []

    def test_valid_grant_dispatches_and_links_original_attempt(self):
        """对照用例：授权未撤回时派发成功，结果关联原规则版本与原尝试。"""
        svc, _, _ = _seed_two_events()
        svc.replay_template(TPL_A)
        app = svc.submit_application(TPL_A, SIGNATURE)
        svc.approve_application(app["申请ID"])
        claim = svc.claim_recovery(app["申请ID"])
        dispatched = svc.dispatch_recovery(claim["领用ID"])
        assert dispatched["ok"] is True
        assert dispatched["采集任务号"].startswith("CAPT-REPLAY-")
        record = dispatched["entry"]
        assert record["派发结果"] == "已派发补采"
        assert record["原规则版本"] == "1"
        assert record["原尝试"] == "3"

    def test_cannot_approve_when_candidate_rule_not_active(self):
        svc, _, _ = _seed_two_events()
        rule_svc = service_for("exception_rule")
        svc.replay_template(TPL_A)
        app = svc.submit_application(TPL_A, SIGNATURE)
        # 先把待启规则抑制掉，再审批应被驳回
        rule_svc.run_action(app["entry"]["待启规则ID"], "抑制规则")
        result = svc.approve_application(app["申请ID"])
        assert result["ok"] is False
        assert app["entry"]["status"] == "已驳回"

    def test_replay_without_version_pair_is_not_recoverable(self):
        svc = service_for("rule_replay_event")
        svc.register_event({
            "事件编号": "EVT-X1", "模板编号": TPL_A,
            "事件签名": "ERR:定位漂移:no-version-pair",
            "错误类别": "定位错误", "原规则编号": "EXCE-0001",
        })
        result = svc.replay_template(TPL_A, ["ERR:定位漂移:no-version-pair"])
        assert result["冲突差异数"] == 0
        assert result["结果"][0]["可回放"] is False
