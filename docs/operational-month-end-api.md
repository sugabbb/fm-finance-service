# Operational month-end data API (operational-service → finance-service)

**Status:** contract agreed from the finance side; **to be implemented in operational-service.**
Finance already calls it (`app/integrations/operational.py`). Until it exists, every monthly
cut-off waits (see "404" below).

## Why

The monthly cut-off reports a **potential revenue**: assets that will become revenue later. These
are the egg stock and the fish still in the ponds at the end of the month. The quantities are
operational data, and operational-service owns them. Finance never reads the operational
database. It reads the quantities through this private API and values them itself: the price,
valuation and reporting happen **only in finance**. Operational sends quantities only, never
prices or amounts.

A month is closed only once both datasets are available. Until then, the finance cut-off of
that month waits.

## Common rules

| | |
|---|---|
| Caller | finance-service only, server to server, over the private network |
| Auth | header `X-FM-Internal-Key: <INTERNAL_API_KEY>` (the same key as the other `/api/v1/internal/*` endpoints) |
| Method | `GET`, read only, no body |
| Path params | `year`: 4 digits, e.g. `2026`. `month`: `1`-`12`. Finance sends it without padding (`7`); accepting `07` too is recommended |
| Timezone | months and "end of the month" are calendar months in `APP_TIMEZONE` (`Asia/Jakarta`), as in finance |
| Numbers | weights are **decimal strings** with at most 3 decimals (`"120.500"`), never floats; counts are integers |
| Unknown fields | finance ignores fields it does not know, so operational may add some without coordinating |

### Finalization, `404`, and revisions

- **`404` = not finalized yet.** Answer `404` for a month until its data is final: the month has
  not ended, the stock count or the sampling is not done, or the data is not approved. Body:
  ```json
  {"code": "MONTH_END_NOT_FINALIZED", "detail": "The egg stock of 2026-07 has not been finalized yet."}
  ```
  Finance treats **any** `404` as "not available yet" and tries again later (hourly). The `code`
  and `detail` are copied into finance's logs, so make them explain *what* is missing.
- **`200` = final.** Once a month answers `200`, it must never go back to `404`.
- **Corrections** after finalization: return the corrected figures with `revision` increased
  by 1 and `finalized_at` set to the time of the correction. Finance records the `revision` it
  used for every cut-off. **Finance does not restate on its own:** after a correction, finance
  re-runs that month (`python -m app.scripts.monthly_cutoff --month YYYY-MM`). Tell the finance
  team when you correct a month they have already closed.
- Any other error (`401`/`403` wrong key, `422` invalid path, `5xx`) is treated as a **failure**
  and is never read as "not available". The cut-off run fails (exit code 1) and the next hourly run
  tries again.

Both datasets have the same envelope:

| field | type | rule |
|---|---|---|
| `year`, `month` | int | **must equal the path**. Finance rejects data for another month |
| `revision` | int ≥ 1 | starts at 1, +1 per correction |
| `finalized_at` | ISO-8601 datetime **with offset** | when this revision was finalized |

---

## 1. Egg stock at the end of the month

```
GET /api/v1/internal/month-end/{year}/{month}/egg-stock
```

The **closing egg stock** at the end of the month's last day (23:59:59, Asia/Jakarta). It includes
every movement dated up to that day: production, breakage/reclassification, and sales.

Egg sales are recorded in finance and reach operational through the inventory outbox
(`PUT /api/v1/internal/inventory/egg-sales/{revenue_id}`). **Finalize a month only after the egg
sales dated in it have been applied.** Finance checks its outbox when it closes the month. If
an egg sale dated in or before the month had not been delivered when `finalized_at` was set,
finance still closes the month but records a warning that the stock may be wrong.

```json
{
  "data": {
    "year": 2026,
    "month": 7,
    "revision": 1,
    "finalized_at": "2026-08-01T09:15:00+07:00",
    "lines": [
      {"egg_category": "UTUH",   "weight_kg": "120.500", "quantity_pieces": 1900},
      {"egg_category": "PECAH",  "weight_kg": "3.250",   "quantity_pieces": 55},
      {"egg_category": "REJECT", "weight_kg": "0.000",   "quantity_pieces": 0}
    ]
  }
}
```

| field | type | rule |
|---|---|---|
| `lines[].egg_category` | string | `UTUH`, `PECAH` or `REJECT` (same codes as finance egg sales); each at most once |
| `lines[].weight_kg` | decimal string | ≥ 0, ≤ 3 decimals. **Required**: finance prices eggs per kg |
| `lines[].quantity_pieces` | int | ≥ 0 |

Send all three categories, with zeros when there is no stock. A missing category counts as no
stock. Stock that is genuinely empty is `200` with zeros, **not** `404`.

## 2. Fish weight estimate at the end of the month

```
GET /api/v1/internal/month-end/{year}/{month}/fish-weight-estimate
```

The estimated **live fish weight in each pond** at the end of the month, e.g. from the latest
sampling (estimated count × average weight), adjusted for mortality and harvests up to the
month's end.

```json
{
  "data": {
    "year": 2026,
    "month": 7,
    "revision": 1,
    "finalized_at": "2026-08-01T10:00:00+07:00",
    "ponds": [
      {
        "pond_id": 3,
        "pond_code": "K-01",
        "pond_name": "Kolam 1",
        "estimated_weight_kg": "576.000",
        "estimated_fish_count": 4800,
        "average_weight_g": "120.000",
        "sampled_on": "2026-07-29"
      },
      {"pond_id": 4, "pond_code": "K-02", "pond_name": "Kolam 2", "estimated_weight_kg": "0.000", "estimated_fish_count": 0}
    ]
  }
}
```

| field | type | rule |
|---|---|---|
| `ponds[].pond_id` | int | the operational pond id (finance stores it as a plain reference) |
| `ponds[].pond_code` | string, 1-60 | unique in the response |
| `ponds[].pond_name` | string, 1-255 | |
| `ponds[].estimated_weight_kg` | decimal string | ≥ 0, ≤ 3 decimals. **Required**: the only figure finance values |
| `ponds[].estimated_fish_count` | int or omitted | ≥ 0, informational |
| anything else (`average_weight_g`, `sampled_on`, ...) | | ignored by finance, useful for people reading operational |

Empty ponds may be omitted or sent with `"0.000"`. No fish at all is `200` with `"ponds": []`.

---

## What finance does with it (for reference)

- **When:** `python -m app.scripts.monthly_cutoff --pending` runs every hour. While the month
  that just ended has no cut-off, it asks for **both** datasets of that month. If either answers
  `404`, nothing is computed or recorded, and the command exits with `3` ("waiting"). Once both
  answer `200`, the month is closed and the hourly runs stop asking.
- **Validation:** a response that breaks the rules above fails the cut-off and is never used.
  Examples: another month, a negative or 4-decimal weight, an unknown or duplicated egg
  category, a duplicated pond, or a `finalized_at` without an offset.
- **Valuation:** `value = weight_kg × average sales price per kg`. The price comes from finance's
  own sales of the same product (per egg category; all fish) dated in the month. If the month
  had none, finance uses the latest of the 3 months before it. Without any such sale, the line
  is worth 0 and a warning is recorded.
- The result is reported in `GET /api/v1/monthly-cutoffs` as `potential_revenue`, apart from cash
  and from the cash-basis P&L.
