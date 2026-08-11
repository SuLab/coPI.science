#!/usr/bin/env python3
"""
Check (and optionally provision) the AWS/DNS infrastructure for inbound email
replies — the review+TOKEN@reply.copi.science flow.

Background (investigation of 2026-08-11)
-----------------------------------------
The reply-by-email review flow shipped in code but its infrastructure was
never provisioned on prod. Every layer was missing, so PI replies bounced and
nothing was processed:

  1. DNS: reply.copi.science had NO MX record (replies never reached AWS).
  2. S3: the copi-inbound-email bucket did not exist.
  3. SES: no receipt rule delivered mail for the reply domain to S3.
  4. IAM: copi-ec2-ses-role had send-only perms (no S3 read/delete for polling).
  5. Env: ENABLE_INBOUND_EMAIL was unset, so the worker never polled anyway.

This script verifies each layer (--check, the default) and can create the AWS
pieces (--provision). DNS records must be added at the registrar by hand; the
script prints exactly what to add.

Prerequisites
-------------
Run from a machine/profile with ADMIN AWS credentials (SES receipt rules, S3
bucket creation, IAM read). The EC2 instance role is NOT sufficient — that is
finding #4 above.

Usage
-----
  # Report the state of every layer, change nothing:
  python scripts/setup_inbound_email.py --check

  # Create bucket + policy + receipt rule set/rule, then print DNS + IAM steps:
  python scripts/setup_inbound_email.py --provision

  # Non-default names:
  python scripts/setup_inbound_email.py --check \
      --region us-east-2 --bucket copi-inbound-email \
      --prefix inbound/ --reply-domain reply.copi.science

After provisioning
------------------
  1. Add the printed MX (and, if newly verifying the domain, TXT) records.
  2. Attach the printed IAM policy to the instance role (copi-ec2-ses-role).
  3. Set ENABLE_INBOUND_EMAIL=true in the prod .env and recreate the worker:
       docker compose -f docker-compose.prod.yml -f docker-compose.override.yml \
         up -d worker
  4. Send a test reply and watch: docker logs -f copi-python-worker-1
"""

import argparse
import json
import subprocess
import sys

RULE_SET_NAME = "copi-inbound"
RULE_NAME = "copi-reply-to-s3"


def _print(status: str, layer: str, detail: str) -> None:
    print(f"  [{status:^4}] {layer}: {detail}")


def check_mx(reply_domain: str, region: str) -> bool:
    """MX must point at SES inbound SMTP for the region."""
    expected = f"inbound-smtp.{region}.amazonaws.com"
    try:
        out = subprocess.run(
            ["dig", "+short", "MX", reply_domain],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _print("SKIP", "DNS", f"`dig` unavailable — check manually that {reply_domain} "
                              f"has MX 10 {expected}")
        return False
    if expected in out:
        _print("OK", "DNS", f"MX for {reply_domain} → {expected}")
        return True
    _print("FAIL", "DNS", f"no MX for {reply_domain} pointing at {expected} "
                          f"(got: {out or 'no MX record at all'})")
    print(f"         Add at the registrar:  {reply_domain}.  MX  10  {expected}.")
    return False


def check_bucket(s3, bucket: str) -> bool:
    try:
        s3.head_bucket(Bucket=bucket)
        _print("OK", "S3", f"bucket {bucket} exists and is reachable")
        return True
    except Exception as exc:
        _print("FAIL", "S3", f"bucket {bucket}: {exc}")
        return False


def check_identity(ses, reply_domain: str) -> bool:
    try:
        attrs = ses.get_identity_verification_attributes(Identities=[reply_domain])
        status = (
            attrs["VerificationAttributes"]
            .get(reply_domain, {})
            .get("VerificationStatus", "NotFound")
        )
    except Exception as exc:
        _print("SKIP", "SES identity", f"cannot query ({exc})")
        return False
    if status == "Success":
        _print("OK", "SES identity", f"{reply_domain} is verified")
        return True
    _print("FAIL", "SES identity", f"{reply_domain} verification status: {status}")
    return False


def check_receipt_rule(ses, bucket: str, reply_domain: str) -> bool:
    try:
        active = ses.describe_active_receipt_rule_set()
    except Exception as exc:
        _print("SKIP", "SES receipt", f"cannot query receipt rule sets ({exc})")
        return False
    for rule in active.get("Rules", []):
        recipients = rule.get("Recipients", [])
        domain_match = not recipients or any(
            r == reply_domain or r.endswith("@" + reply_domain) for r in recipients
        )
        s3_actions = [a["S3Action"] for a in rule.get("Actions", []) if "S3Action" in a]
        if rule.get("Enabled") and domain_match and any(
            a["BucketName"] == bucket for a in s3_actions
        ):
            _print("OK", "SES receipt",
                   f"active rule '{rule['Name']}' delivers {reply_domain} → s3://{bucket}")
            return True
    name = (active.get("Metadata") or {}).get("Name")
    _print("FAIL", "SES receipt",
           f"active rule set {name or '(none)'} has no enabled rule delivering "
           f"{reply_domain} to s3://{bucket}")
    return False


def check_env_flag() -> bool:
    """This checks the LOCAL environment only — the flag that matters is the
    one in the prod .env consumed by the worker container."""
    import os

    val = os.environ.get("ENABLE_INBOUND_EMAIL", "")
    if val.lower() in ("1", "true", "yes"):
        _print("OK", "Env", "ENABLE_INBOUND_EMAIL is set here")
    else:
        _print("WARN", "Env",
               "ENABLE_INBOUND_EMAIL not set in this shell — ensure it is "
               "true in the prod .env (worker service) once AWS+DNS are ready")
    return True


def instance_role_policy(bucket: str, prefix: str) -> dict:
    """The statements copi-ec2-ses-role needs for the worker's polling loop
    (read+delete under the inbound prefix, write for failed/ quarantine)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "CopiInboundEmailList",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
            },
            {
                "Sid": "CopiInboundEmailReadWrite",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:DeleteObject", "s3:PutObject"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}/{prefix}*",
                    f"arn:aws:s3:::{bucket}/failed/*",
                ],
            },
        ],
    }


def ses_bucket_policy(bucket: str, account_id: str, region: str) -> dict:
    """Allow SES (this account's receipt rules only) to write into the bucket."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowSESPuts",
                "Effect": "Allow",
                "Principal": {"Service": "ses.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
                "Condition": {
                    "StringEquals": {"AWS:SourceAccount": account_id},
                    "ArnLike": {
                        "AWS:SourceArn": f"arn:aws:ses:{region}:{account_id}:receipt-rule-set/*"
                    },
                },
            }
        ],
    }


def provision(region: str, bucket: str, prefix: str, reply_domain: str) -> None:
    import boto3

    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    s3 = boto3.client("s3", region_name=region)
    ses = boto3.client("ses", region_name=region)

    # 1. Bucket (idempotent) + SES write policy
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"bucket {bucket} already exists")
    except Exception:
        kwargs = {"Bucket": bucket}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
            },
        )
        print(f"created bucket {bucket}")
    s3.put_bucket_policy(
        Bucket=bucket, Policy=json.dumps(ses_bucket_policy(bucket, account_id, region))
    )
    print("attached SES write policy to bucket")

    # 2. Domain identity for receiving (prints the TXT record if new)
    attrs = ses.get_identity_verification_attributes(Identities=[reply_domain])
    status = (
        attrs["VerificationAttributes"].get(reply_domain, {}).get("VerificationStatus")
    )
    if status != "Success":
        token = ses.verify_domain_identity(Domain=reply_domain)["VerificationToken"]
        print(f"requested domain verification for {reply_domain}; add DNS record:")
        print(f'  _amazonses.{reply_domain}.  TXT  "{token}"')

    # 3. Receipt rule set + rule (idempotent), then activate
    try:
        ses.create_receipt_rule_set(RuleSetName=RULE_SET_NAME)
        print(f"created receipt rule set {RULE_SET_NAME}")
    except ses.exceptions.AlreadyExistsException:
        print(f"receipt rule set {RULE_SET_NAME} already exists")
    rule = {
        "Name": RULE_NAME,
        "Enabled": True,
        "Recipients": [reply_domain],
        "Actions": [
            {
                "S3Action": {
                    "BucketName": bucket,
                    "ObjectKeyPrefix": prefix,
                }
            }
        ],
        "ScanEnabled": True,
        "TlsPolicy": "Optional",
    }
    try:
        ses.create_receipt_rule(RuleSetName=RULE_SET_NAME, Rule=rule)
        print(f"created receipt rule {RULE_NAME}")
    except ses.exceptions.AlreadyExistsException:
        ses.update_receipt_rule(RuleSetName=RULE_SET_NAME, Rule=rule)
        print(f"updated receipt rule {RULE_NAME}")
    active = ses.describe_active_receipt_rule_set().get("Metadata") or {}
    if active.get("Name") != RULE_SET_NAME:
        if active.get("Name"):
            print(f"WARNING: replacing active rule set {active['Name']!r} — its rules "
                  f"stop matching. Merge them into {RULE_SET_NAME} first if needed.")
        ses.set_active_receipt_rule_set(RuleSetName=RULE_SET_NAME)
        print(f"activated receipt rule set {RULE_SET_NAME}")

    # 4. What cannot be done from here
    print("\nRemaining manual steps:")
    print(f"  1. Registrar DNS:  {reply_domain}.  MX  10  "
          f"inbound-smtp.{region}.amazonaws.com.")
    print("  2. Attach this policy to the EC2 instance role (copi-ec2-ses-role):")
    print(json.dumps(instance_role_policy(bucket, prefix), indent=4))
    print("  3. Set ENABLE_INBOUND_EMAIL=true in the prod .env and recreate the worker.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", default=False)
    ap.add_argument("--provision", action="store_true", default=False)
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--bucket", default="copi-inbound-email")
    ap.add_argument("--prefix", default="inbound/")
    ap.add_argument("--reply-domain", default="reply.copi.science")
    args = ap.parse_args()

    if args.provision:
        provision(args.region, args.bucket, args.prefix, args.reply_domain)
        return 0

    # Default: --check
    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    ses = boto3.client("ses", region_name=args.region)
    print(f"Inbound email infrastructure check ({args.reply_domain} → "
          f"s3://{args.bucket}/{args.prefix} in {args.region}):")
    results = [
        check_mx(args.reply_domain, args.region),
        check_identity(ses, args.reply_domain),
        check_receipt_rule(ses, args.bucket, args.reply_domain),
        check_bucket(s3, args.bucket),
        check_env_flag(),
    ]
    if all(results):
        print("All layers OK.")
        return 0
    print("\nOne or more layers missing — run with --provision (admin creds) "
          "and follow the printed manual steps.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
