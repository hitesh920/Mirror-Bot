"""Cloudflare account analytics used by the owner-only R2 stats command."""

from __future__ import annotations

import calendar
import json
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..core.config import Config

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

CLASS_A_ACTIONS = {
    "CompleteMultipartUpload",
    "CopyObject",
    "CreateMultipartUpload",
    "LifecycleStorageTierTransition",
    "ListBuckets",
    "ListMultipartUploads",
    "ListObjects",
    "ListParts",
    "PutBucket",
    "PutBucketCors",
    "PutBucketEncryption",
    "PutBucketLifecycleConfiguration",
    "PutObject",
    "UploadPart",
    "UploadPartCopy",
}

CLASS_B_ACTIONS = {
    "GetBucketCors",
    "GetBucketEncryption",
    "GetBucketLifecycleConfiguration",
    "GetBucketLocation",
    "GetObject",
    "HeadBucket",
    "HeadObject",
    "UsageSummary",
}

R2_ANALYTICS_QUERY = """
query R2Stats(
  $accountTag: string!
  $startDate: Time
  $endDate: Time
  $bucketName: string
) {
  viewer {
    accounts(filter: { accountTag: $accountTag }) {
      r2OperationsAdaptiveGroups(
        limit: 10000
        filter: {
          datetime_geq: $startDate
          datetime_leq: $endDate
          bucketName: $bucketName
        }
      ) {
        sum { requests }
        dimensions { actionType }
      }
      r2StorageAdaptiveGroups(
        limit: 1
        filter: {
          datetime_geq: $startDate
          datetime_leq: $endDate
          bucketName: $bucketName
        }
        orderBy: [datetime_DESC]
      ) {
        max {
          objectCount
          uploadCount
          payloadSize
          metadataSize
        }
        dimensions { datetime }
      }
    }
  }
}
"""


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _month_anchor(reference: datetime, month_offset: int, day: int) -> datetime:
    month_index = reference.year * 12 + reference.month - 1 + month_offset
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    clamped_day = min(day, calendar.monthrange(year, month)[1])
    return datetime(
        year,
        month,
        clamped_day,
        reference.hour,
        reference.minute,
        reference.second,
        tzinfo=UTC,
    )


def billing_period(
    anchor: datetime,
    now: datetime,
    subscription_start: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return the current monthly period for a subscription anchor."""

    anchor = anchor.astimezone(UTC)
    now = now.astimezone(UTC)
    current_anchor = _month_anchor(
        anchor, (now.year - anchor.year) * 12 + now.month - anchor.month, anchor.day
    )
    if now < current_anchor:
        start = _month_anchor(current_anchor, -1, anchor.day)
        end = current_anchor
    else:
        start = current_anchor
        end = _month_anchor(current_anchor, 1, anchor.day)
    if subscription_start is not None:
        start = max(start, subscription_start.astimezone(UTC))
    return start, end


def classify_operations(groups: list[dict]) -> tuple[int, int]:
    """Map Cloudflare R2 action types to their documented billing classes."""

    class_a = 0
    class_b = 0
    for group in groups:
        action = str(group.get("dimensions", {}).get("actionType") or "")
        requests = int(group.get("sum", {}).get("requests") or 0)
        if action in CLASS_A_ACTIONS:
            class_a += requests
        elif action in CLASS_B_ACTIONS:
            class_b += requests
    return class_a, class_b


def _request_json(
    url: str,
    token: str,
    payload: dict | None = None,
) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            details = json.load(exc)
            message = "; ".join(
                str(item.get("message") or item) for item in details.get("errors", [])
            )
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            message = exc.reason
        raise RuntimeError(
            f"Cloudflare API returned HTTP {exc.code}: {message}"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Cloudflare API request failed: {exc}") from exc

    errors = result.get("errors") or []
    if errors:
        message = "; ".join(str(item.get("message") or item) for item in errors)
        raise RuntimeError(f"Cloudflare API error: {message}")
    if result.get("success") is False:
        raise RuntimeError("Cloudflare API request was unsuccessful")
    return result


def _subscription_period(
    config: Config,
    now: datetime,
) -> tuple[datetime, datetime]:
    response = _request_json(
        f"{CLOUDFLARE_API}/accounts/{config.cloudflare_account_id}/paygo-usage-info",
        config.cloudflare_api_token,
    )
    subscriptions = response.get("result", {}).get("subscriptions") or []
    active = [
        item
        for item in subscriptions
        if not item.get("end_timestamp") or _parse_time(item["end_timestamp"]) > now
    ]
    if not active:
        raise RuntimeError("Cloudflare returned no active billing subscription")
    subscription = max(
        active,
        key=lambda item: item.get("start_timestamp") or "",
    )
    return billing_period(
        _parse_time(subscription["billing_cycle_anchor_timestamp"]),
        now,
        _parse_time(subscription["start_timestamp"]),
    )


def _billable_usage(
    config: Config,
    period_start: datetime,
    period_end: datetime,
) -> tuple[float, str]:
    query = urlencode(
        {
            "from": period_start.date().isoformat(),
            "to": period_end.date().isoformat(),
        }
    )
    response = _request_json(
        f"{CLOUDFLARE_API}/accounts/{config.cloudflare_account_id}/paygo-usage?{query}",
        config.cloudflare_api_token,
    )
    records = [
        item
        for item in response.get("result") or []
        if str(item.get("ServiceFamilyName") or "").casefold() == "r2"
        or "r2" in str(item.get("ServiceName") or "").casefold()
    ]
    cost = sum(float(item.get("ContractedCost") or 0) for item in records)
    currency = next(
        (
            str(item["BillingCurrency"])
            for item in records
            if item.get("BillingCurrency")
        ),
        "USD",
    )
    return cost, currency


def r2_account_usage(
    config: Config,
    now: datetime | None = None,
) -> dict:
    """Fetch current-period R2 operations, storage, and billable usage."""

    if not config.cloudflare_analytics_configured:
        raise RuntimeError("Cloudflare account analytics is not configured")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    period_start, period_end = _subscription_period(config, now)
    response = _request_json(
        f"{CLOUDFLARE_API}/graphql",
        config.cloudflare_api_token,
        {
            "query": R2_ANALYTICS_QUERY,
            "variables": {
                "accountTag": config.cloudflare_account_id,
                "startDate": period_start.isoformat(),
                "endDate": min(now, period_end).isoformat(),
                "bucketName": config.r2_bucket,
            },
        },
    )
    accounts = response.get("data", {}).get("viewer", {}).get("accounts") or []
    if not accounts:
        raise RuntimeError("Cloudflare returned no R2 analytics account")
    account = accounts[0]
    class_a, class_b = classify_operations(
        account.get("r2OperationsAdaptiveGroups") or []
    )
    storage_groups = account.get("r2StorageAdaptiveGroups") or []
    storage = storage_groups[0].get("max", {}) if storage_groups else {}
    cost, currency = _billable_usage(config, period_start, period_end)
    return {
        "period_start": period_start,
        "period_end": period_end,
        "class_a": class_a,
        "class_b": class_b,
        "bytes": int(storage.get("payloadSize") or 0),
        "objects": int(storage.get("objectCount") or 0),
        "billable_cost": cost,
        "currency": currency,
    }
