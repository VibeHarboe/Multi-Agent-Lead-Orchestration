"""CLI entry point. Usage:

    python -m src.cli --dry-run RL00046            # Week 3: plumbing walk, no LLM
    python -m src.cli --run RL00046                # Week 4: full graph + checkpointer
    python -m src.cli --run RL00046 --replies counter counter decline
    python -m src.cli --paused RL00046             # show the pending interrupt
    python -m src.cli --resume RL00046 --action enrich --field service_type=audit
    python -m src.cli --resume RL00046 --action reactivate --partner P018
    python -m src.cli --resume RL00046 --action override --partner P012
    python -m src.cli --resume RL00046 --action drop --note "duplicate"
    python -m src.cli --find-lead SE               # pick a seeded subject
    python -m src.cli --list-tools                 # MCP contract at a glance

Every path here runs with the deterministic TemplateDrafter — no anthropic
import, no API key. The live LLM paths (ClaudeDrafter, --scan) arrive in
Week 5+.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import load_config

_RULE = "=" * 72


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Multi-Agent Lead Orchestration")
    parser.add_argument("--dry-run", metavar="LEAD_ID",
                        help="Walk one seeded referral through the graph "
                             "against MCP mocks — no LLM, no API key.")
    parser.add_argument("--replies", nargs="*", default=None,
                        help="Scripted slack replies for the dry-run "
                             "(accept | decline | counter | no_reply).")
    parser.add_argument("--find-lead", metavar="COUNTRY",
                        help="List seeded pending/matching referrals for a "
                             "market (dry-run subjects).")
    parser.add_argument("--list-tools", action="store_true",
                        help="List every tool on the three MCP servers.")
    parser.add_argument("--as-of", default=None,
                        help="Override the as-of date (ISO). Defaults to "
                             "AS_OF_DATE from .env.")
    parser.add_argument("--top-k", type=int, default=None,
                        help="Override MATCHING_TOP_K.")
    parser.add_argument("--run", metavar="LEAD_ID",
                        help="Run one seeded referral through the full graph "
                             "with the SQLite checkpointer (pauses at HITL).")
    parser.add_argument("--paused", metavar="LEAD_ID",
                        help="Show the pending interrupt payload for a lead.")
    parser.add_argument("--resume", metavar="LEAD_ID",
                        help="Resume a paused lead with a manager decision.")
    parser.add_argument("--action",
                        choices=["enrich", "reactivate", "override", "drop"],
                        help="With --resume: the manager's decision.")
    parser.add_argument("--partner", metavar="PARTNER_ID",
                        help="With --resume reactivate/override.")
    parser.add_argument("--field", action="append", default=[],
                        metavar="NAME=VALUE",
                        help="With --resume enrich (repeatable), e.g. "
                             "--field country=SE --field service_type=audit")
    parser.add_argument("--note", default=None,
                        help="With --resume: free-text manager note.")
    parser.add_argument("--scan", action="store_true",
                        help="Run the Monitor's daily sweep: SLA breaches, "
                             "partner interventions, market imbalances.")
    parser.add_argument("--reinject", action="store_true",
                        help="With --scan: re-run each close-SLA-breached "
                             "deal through the graph, excluding the "
                             "breaching partner (§3.4a).")
    parser.add_argument("--post-digest", action="store_true",
                        help="With --scan: post the daily digest to the "
                             "(mock) Slack ops channel.")
    parser.add_argument("--weekly-report", action="store_true",
                        help="Render the Monday per-partner report (Q12).")
    args = parser.parse_args(argv)

    config = load_config(require_api_key=False)
    as_of = date.fromisoformat(args.as_of) if args.as_of else (
        config.as_of_date or date.today())
    top_k = args.top_k or config.matching_top_k

    if args.list_tools:
        from .mcp_stack import MCPStack
        stack = MCPStack(config.duckdb_path)
        for server in ("partner_capacity", "slack", "crm"):
            tools = stack.list_tools(server)
            print(f"{server}  ({len(tools)} tools)")
            for t in tools:
                print(f"  - {t}")
        return 0

    if args.find_lead:
        from .data.warehouse import find_referral_lead
        rows = find_referral_lead(config.duckdb_path,
                                  country=args.find_lead.upper(),
                                  status="pending", limit=10)
        if not rows:
            rows = find_referral_lead(config.duckdb_path,
                                      country=args.find_lead.upper(),
                                      status="matching", limit=10)
        for r in rows:
            print(f"{r['referral_lead_id']}  {r['country']}  "
                  f"{r['service_type'] or '(?)':14}  {r['industry'] or '(?)':22}  "
                  f"status={r['referral_status']}")
        if not rows:
            print(f"no pending/matching referrals seeded for "
                  f"{args.find_lead.upper()}", file=sys.stderr)
        return 0

    if args.dry_run:
        from .dry_run import dry_run
        print(f"{_RULE}\nDRY-RUN {args.dry_run}   (as-of {as_of}, top-{top_k}, "
              f"no LLM)\n{_RULE}")
        try:
            result = dry_run(args.dry_run,
                             warehouse_path=config.duckdb_path,
                             top_k=top_k, as_of=as_of,
                             scripted_replies=args.replies)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for line in result.walk_log:
            print(line)
        if result.dormant_partners:
            print(f"\nreactivation candidates:")
            for d in result.dormant_partners:
                print(f"  {d['partner_id']}  {d['partner_name']}  "
                      f"spec={d['specialization_strength']:.0f}")
        print(f"\n{_RULE}\nOUTCOME: {result.outcome}"
              + (f"  ({result.lead.hitl_reason})" if result.lead.hitl_reason else ""))
        return 0 if result.outcome == "booked" else 2

    if args.scan:
        from .agents.monitor import escalate_breaches, post_digest, sweep
        from .mcp_stack import MCPStack
        from .observability import TraceRecorder

        recorder = TraceRecorder()
        stack = MCPStack(config.duckdb_path, recorder=recorder)
        print(f"{_RULE}\nMONITOR SWEEP   (as-of {as_of})\n{_RULE}")
        report = sweep(config.duckdb_path, config, as_of=as_of,
                       recorder=recorder)
        print(f"SLA breaches:   {len(report.sla_breaches)}")
        for b in report.sla_breaches:
            print(f"  • {b.referral_lead_id} ({b.country}) booked {b.booked_at}, "
                  f"{b.days_since_booking}d unresolved — partner "
                  f"{b.breaching_partner_id}")
        print(f"interventions:  {len(report.interventions)}")
        for i in report.interventions:
            print(f"  • {i.kind:16} {i.partner_id} ({i.partner_side}) — {i.detail}")
        print(f"market alerts:  {len(report.market_alerts)}")
        for m in report.market_alerts:
            print(f"  • {m.kind:10} {m.country} at {m.avg_util_pct}% "
                  f"for {m.consecutive_days}d")

        if report.sla_breaches:
            escalate_breaches(report, stack)
            print(f"\n{len(report.sla_breaches)} escalation note(s) written to CRM")
        if args.post_digest:
            post_digest(report, stack)
            print("daily digest posted to #nordledger-ops")

        if args.reinject and report.sla_breaches:
            from .dry_run import lead_state_from_seed
            from .data.warehouse import get_referral_lead_by_id
            from .graph import extract_interrupt, lead_out, open_graph, run_lead
            print(f"\n{_RULE}\nRE-INJECTION   ({len(report.sla_breaches)} deal(s))\n{_RULE}")
            with open_graph(stack, config, as_of=as_of,
                            recorder=recorder) as graph:
                for b in report.sla_breaches:
                    seed = get_referral_lead_by_id(config.duckdb_path,
                                                   b.referral_lead_id)
                    lead = lead_state_from_seed(seed)
                    result = run_lead(graph, lead, recorder=recorder,
                                      exclude_partner_ids=(
                                          [b.breaching_partner_id]
                                          if b.breaching_partner_id else []))
                    pause = extract_interrupt(result)
                    outcome = (f"paused at {pause['destination']}" if pause
                               else lead_out(result).status)
                    new_partner = ("" if pause else
                                   f" → new partner "
                                   f"{lead_out(result).negotiation_history[-1]['partner_id']}")
                    print(f"  {b.referral_lead_id}: {outcome}{new_partner} "
                          f"(excluded {b.breaching_partner_id})")

        recorder.save()
        return 2 if report.has_findings else 0

    if args.weekly_report:
        from .agents.monitor import build_weekly_report, post_weekly_report
        from .mcp_stack import MCPStack
        print(f"{_RULE}\nWEEKLY PARTNER REPORT   (week ending {as_of})\n{_RULE}")
        report = build_weekly_report(config.duckdb_path, config, as_of=as_of)
        for row in report["partners"]:
            print(f"  {row['narrative']}")
            print(f"    ROI trend {row['roi_trend']} · churn {row['churn_trend']} · "
                  f"{row['accepts']}A / {row['declines']}D / "
                  f"{row['no_responses']}N / {row['cancellations']}C")
        stack = MCPStack(config.duckdb_path)
        post_weekly_report(report, stack)
        print(f"\nposted to #nordledger-weekly ({len(report['partners'])} partners)")
        return 0

    if args.run or args.paused or args.resume:
        import json

        from .dry_run import lead_state_from_seed
        from .data.warehouse import get_referral_lead_by_id
        from .graph import (extract_interrupt, get_paused, open_graph,
                            resume_lead, run_lead)
        from .mcp_stack import MCPStack

        stack = MCPStack(config.duckdb_path)
        if args.replies:
            stack.prime_slack_replies(args.replies)

        def _print_outcome(result) -> int:
            pause = extract_interrupt(result)
            if pause:
                print(f"\n{_RULE}\nPAUSED at {pause['destination']}\n{_RULE}")
                print(f"reason:  {pause['hitl_reason']}")
                print(f"options: {', '.join(pause['options'])}")
                if pause.get("dormant_partners"):
                    print("reactivation candidates:")
                    for d in pause["dormant_partners"]:
                        print(f"  {d['partner_id']}  {d['partner_name']}")
                print(f"\nresume with: python -m src.cli --resume "
                      f"{pause['referral_lead_id']} --action <...>")
                return 2
            lead = result["lead"]
            status = lead["status"] if isinstance(lead, dict) else lead.status
            print(f"\n{_RULE}\nOUTCOME: {status}\n{_RULE}")
            history = (lead["negotiation_history"] if isinstance(lead, dict)
                       else lead.negotiation_history)
            for h in history:
                print(f"  round {h['round']} · {h['partner_id']} · "
                      f"{h['outcome']} · tier={h.get('model_tier')}")
            return 0 if status == "booked" else 2

        if args.paused:
            with open_graph(stack, config, as_of=as_of) as graph:
                pause = get_paused(graph, args.paused)
            if pause is None:
                print(f"{args.paused} is not paused")
                return 1
            print(json.dumps(pause, indent=2, default=str))
            return 0

        if args.run:
            seed = get_referral_lead_by_id(config.duckdb_path, args.run)
            if seed is None:
                print(f"error: unknown referral_lead_id {args.run}",
                      file=sys.stderr)
                return 1
            lead = lead_state_from_seed(seed)
            print(f"{_RULE}\nRUN {args.run}   (as-of {as_of}, "
                  f"checkpointed, TemplateDrafter)\n{_RULE}")
            with open_graph(stack, config, as_of=as_of) as graph:
                result = run_lead(graph, lead)
            return _print_outcome(result)

        # --resume
        if not args.action:
            parser.error("--resume requires --action")
        decision: dict = {"action": args.action, "note": args.note}
        if args.partner:
            decision["partner_id"] = args.partner
        if args.field:
            decision["fields"] = dict(f.split("=", 1) for f in args.field)
        print(f"{_RULE}\nRESUME {args.resume}   decision={args.action}\n{_RULE}")
        with open_graph(stack, config, as_of=as_of) as graph:
            result = resume_lead(graph, args.resume, decision)
        return _print_outcome(result)

    parser.error("choose one of --dry-run / --run / --resume / --paused / "
                 "--find-lead / --list-tools")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
