#!/usr/bin/env python3
"""Monthly AWS cost model for the platform at baseline and at 10x load, plus the saving from each
implemented optimisation.

THIS IS AN ESTIMATE, NOT A BILL. Unit prices are public on-demand list prices for us-east-1, rounded,
multiplied by REGION_MULTIPLIER (af-south-1 is priced above us-east-1; check the AWS Pricing Calculator and
set it). Replace the "estimate" column in docs/DESIGN.md with real figures from Cost Explorer
(group by service) after the evidence run - the marker asks for ACTUAL spend documented.

    python scripts/cost_model.py            # table
    python scripts/cost_model.py --json
"""
import argparse
import json

REGION_MULTIPLIER = 1.0
HOURS = 730

P = dict(
    fargate_vcpu_h=0.04048, fargate_gb_h=0.004445,
    alb_h=0.0225, alb_lcu_h=0.008, ipv4_h=0.005,
    nat_h=0.045, nat_gb=0.045,
    acu_h=0.12, aurora_gb_month=0.10, aurora_io_per_m=0.20, aurora_backup_gb=0.021,
    sfn_express_req_m=1.00, sfn_express_gb_s=0.00001667, sfn_std_transition_m=25.00,
    lambda_req_m=0.20, lambda_gb_s=0.0000166667,
    logs_ingest_gb=0.50, logs_store_gb=0.03,
    kms_key=1.00, kms_req_10k=0.03,
    secret=0.40, cw_alarm=0.10, cw_dashboard=3.00, cw_metric=0.30,
    xray_per_m=5.00, vpce_h=0.01, container_insights_per_task=1.5,
)


def model(txn_per_month, avg_tasks, avg_acu, nat_gb, log_gb, nat_gws=1, endpoints=0, sfn="express"):
    c = {}
    vcpu, gb = 0.25, 0.5
    c["ECS Fargate (API tasks)"] = avg_tasks * HOURS * (vcpu * P["fargate_vcpu_h"] + gb * P["fargate_gb_h"])
    reqs = txn_per_month * 3          # create txn + status reads + wallet reads per transaction
    lcu = 1 + reqs / 4_000_000       # 1 LCU floor; ~1 extra LCU per 4M requests/month of mixed traffic
    c["Application Load Balancer"] = HOURS * (P["alb_h"] + lcu * P["alb_lcu_h"]) + 2 * HOURS * P["ipv4_h"]
    c["NAT gateway(s)"] = nat_gws * HOURS * P["nat_h"] + nat_gb * P["nat_gb"] + nat_gws * HOURS * P["ipv4_h"]
    c["Interface VPC endpoints"] = endpoints * 2 * HOURS * P["vpce_h"]
    c["Aurora Serverless v2 compute"] = avg_acu * HOURS * P["acu_h"]
    storage_gb = 5 + txn_per_month * 4 / 1e6          # ~4 KB of rows per transaction (ledger, txn, outbox)
    c["Aurora storage, I/O, backup"] = storage_gb * (P["aurora_gb_month"] + P["aurora_backup_gb"]) + txn_per_month * 40 / 1e6 * P["aurora_io_per_m"]
    if sfn == "express":
        gb_s = txn_per_month * 0.4 * 0.0625
        c["Step Functions (Express)"] = txn_per_month / 1e6 * P["sfn_express_req_m"] + gb_s * P["sfn_express_gb_s"]
    else:
        c["Step Functions (Standard)"] = txn_per_month * 8 / 1e6 * P["sfn_std_transition_m"]
    invocations = txn_per_month * 8
    c["Lambda"] = invocations / 1e6 * P["lambda_req_m"] + invocations * 0.12 * 0.25 * P["lambda_gb_s"]
    c["CloudWatch (logs, metrics, alarms, dashboard)"] = (
        log_gb * P["logs_ingest_gb"] + log_gb * 14 / 30 * P["logs_store_gb"]
        + 8 * P["cw_alarm"] + P["cw_dashboard"] + 6 * P["cw_metric"] + avg_tasks * P["container_insights_per_task"])
    c["X-Ray (5% sampling)"] = max(0.0, txn_per_month * 3 * 0.05 / 1e6 - 0.1) * P["xray_per_m"]
    c["KMS (3 keys + requests)"] = 3 * P["kms_key"] + txn_per_month * 6 / 1e4 * P["kms_req_10k"]
    c["Secrets Manager + SQS/SNS + S3 + CloudTrail + ECR"] = P["secret"] + 1.0 + 1.5 + 1.0 + 0.5
    return {k: round(v * REGION_MULTIPLIER, 2) for k, v in c.items()}


SCENARIOS = {
    "baseline (100k txn/month, ~6 tx/s peak)": dict(txn_per_month=100_000, avg_tasks=2, avg_acu=1.0, nat_gb=2, log_gb=1.0),
    "10x (1M txn/month, ~60 tx/s peak)": dict(txn_per_month=1_000_000, avg_tasks=5, avg_acu=3.0, nat_gb=15, log_gb=10.0),
}


def optimisations():
    base = SCENARIOS["baseline (100k txn/month, ~6 tx/s peak)"]
    ten = SCENARIOS["10x (1M txn/month, ~60 tx/s peak)"]
    out = []
    # 1. Express instead of Standard Step Functions
    for label, s in (("baseline", base), ("10x", ten)):
        std = model(**s, sfn="standard")["Step Functions (Standard)"]
        exp = model(**s)["Step Functions (Express)"]
        out.append(("1. Step Functions EXPRESS instead of Standard workflow", label, std, exp))
    # 2. Single NAT + no interface endpoints vs NAT per AZ + 9 interface endpoints
    for label, s in (("baseline", base), ("10x", ten)):
        before = model(**s, nat_gws=2, endpoints=9)
        after = model(**s)
        b = before["NAT gateway(s)"] + before["Interface VPC endpoints"]
        a = after["NAT gateway(s)"] + after["Interface VPC endpoints"]
        out.append(("2. One NAT gateway + endpoints off (vs NAT per AZ + 9 interface endpoints)", label, b, a))
    # 3. Aurora Serverless v2 vs always-on provisioned writer+reader (db.r6g.large, the size needed for the 10x peak)
    prov = 2 * HOURS * 0.26
    for label, s in (("baseline", base), ("10x", ten)):
        out.append(("3. Aurora Serverless v2 instead of 2x provisioned db.r6g.large", label, round(prov, 2), model(**s)["Aurora Serverless v2 compute"]))
    # 4. 14-day log retention instead of never-expire (steady state after 12 months)
    for label, s in (("baseline", base), ("10x", ten)):
        never = s["log_gb"] * 12 * P["logs_store_gb"]
        kept = s["log_gb"] * 14 / 30 * P["logs_store_gb"]
        out.append(("4. CloudWatch log retention 14 days instead of never (after 12 months)", label, never, kept))
    return [(n, load, round(b * REGION_MULTIPLIER, 2), round(a * REGION_MULTIPLIER, 2)) for n, load, b, a in out]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = {k: model(**v) for k, v in SCENARIOS.items()}
    opt = optimisations()
    if a.json:
        print(json.dumps({"scenarios": res, "optimisations": opt}, indent=2))
        raise SystemExit
    names = list(next(iter(res.values())))
    keys = list(res)
    print(f"{'Service':52s}" + "".join(f"{k.split(' (')[0]:>12s}" for k in keys))
    for n in names:
        print(f"{n:52s}" + "".join(f"{res[k][n]:>12.2f}" for k in keys))
    print(f"{'TOTAL (USD/month)':52s}" + "".join(f"{sum(res[k].values()):>12.2f}" for k in keys))
    print(f"\n10x / baseline = {sum(res[keys[1]].values()) / sum(res[keys[0]].values()):.2f}x cost for 10x load\n")
    print(f"{'Optimisation':86s}{'load':>9s}{'before':>10s}{'after':>10s}{'saving':>10s}")
    for n, load, b, af in opt:
        print(f"{n:86s}{load:>9s}{b:>10.2f}{af:>10.2f}{b - af:>10.2f}")
