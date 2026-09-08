# Requests to LynkWell — raised 2026-09-08

Five items. Two are refunds, three are platform changes on LynkWell's side that
we cannot make ourselves. Everything here was verified against our own
`ocpp_events` / `card_transactions` tables and LynkWell's session exports for
August 2026; transaction IDs are LynkWell's own.

---

## 1. Two drivers were charged twice — refunds needed

Both at Glennallen (`as_LYHe6mZTRKiFfziSNJFvJ`). In each case a card settled for
the session **and** the driver's app account was invoiced for the same energy.

| | 11 Aug 2026 | 12 Aug 2026 |
|---|---|---|
| LynkWell session | `se_54kAMdzjSULqf6TDfeu83` | `se_NHrlISwTTUWiBZpoy0NC9` |
| Transaction ID | 1106964 | 1108305 |
| Energy | 60.469 kWh | 51.799 kWh |
| Card settled | **$33.26** (4179 xxxx xxxx 3522) | **$28.49** (4147 xxxx xxxx 2090) |
| App invoiced | **$33.25** (invoice CV6KB6GT-0002) | **$28.48** |
| StartTransaction idTag | `F20AA7178114D0` | `F20AA7178114D0` |

`F20AA7178114D0` is Glennallen's CCR authorisation tag. Both sessions were
started by the card reader, and in both the Nayax tap settled 15 seconds before
the StartTransaction. Utility meter reads for those days show no unaccounted
session, so there is no third session hiding behind either.

### Why it happened, as far as our data shows

**12 Aug** — the app credential was presented and never opened a transaction:

```
00:55:44Z  Authorize  HSCUD4OWBUY0JMKNU1R4    (app token)
00:57:45Z  conn 1 → Available                 (the app start did not take)
00:58:25Z  Authorize  VID:98ed5c81f9cd        (AutoCharge probe, rejected)
00:59:39Z  Nayax tap — settled $28.49
00:59:53Z  StartTransaction  idTag F20AA7178114D0
```

Kris's reading of the LynkWell log inspector puts the first
`RemoteStartTransaction` about 70 seconds before the StartTransaction — long
enough that a timeout is surprising.

**11 Aug** — no `Authorize` reached us at all, and the charger had sent **two
`BootNotification`s in the preceding 25 minutes** (01:14:15Z and 01:39:38Z):

```
01:39:38Z  BootNotification
01:39:39Z  conn 2 → Preparing
01:40:39Z  Nayax tap — settled $33.26
01:40:52Z  StartTransaction  idTag F20AA7178114D0, meterStart 9034422
02:53:07Z  StopTransaction   tx 1106964, meterStop 9094891   → 60,469 Wh
```

**Question:** does a pending remote start survive a websocket reconnect and
attach to the next transaction that arrives on that connector? If so, a flapping
charger will keep producing these, and 11 Aug is the shape to look at.

---

## 2. Add the energy register to `StopTxnSampledData` on CL-A … CL-D

The four Autel HYC400s at Cooper Landing do not include
`Energy.Active.Import.Register` in `StopTransaction.transactionData`. Across all
2,347 StopTransactions we hold:

| Charger | Hardware | `Transaction.Begin` | `Transaction.End` |
|---|---|---|---|
| Glennallen | Autel DL0120 | 100% | 100% |
| Delta - Right | Autel DL0120 | 98% | 98% |
| Delta - Left | Autel DL0120 | 94% | 94% |
| ARG - Left / Right | Tritium Veefil RTM | 0% | 100% |
| **CL-A / B / C / D** | **Autel HYC400** | **0%** | **0%** |

Autel's own OCPP implementation guide for the HYC 50/400 (software 2.5.x) lists
`StopTxnSampledData` as **Read and Write**, with `Energy.Active.Import.Register`
among its supported measurands.

**Request:** a `ChangeConfiguration` on each of the four Cooper Landing units
adding `Energy.Active.Import.Register` to `StopTxnSampledData`, matching what
Glennallen and Delta already send.

This is the cleanest fix for a variance we have been reconstructing in software:
90% of Cooper Landing sessions were coming up short against LynkWell's kWh
because our figure was built from periodic MeterValues, which miss whatever is
delivered between StartTransaction and the first sample, and between the last
sample and StopTransaction.

---

## 3. Turn `DataTransfer` forwarding back on

Our `ocpp_events` table holds 168,784 rows. **75 are `DataTransfer`** — all from
one charger, all on **6 January 2026**, all `RunningCost`. There has never been a
single `FinalCost` row, against 2,346 StopTransactions in the same table.

`FinalCost` carries LynkWell's own billed figure and, critically, the session ID:

```json
{"transactionId":1106964,"cost":33.25,
 "priceText":"TOTAL KWH: 60.469, TOTAL TIME: 1.2272 HRS. ENERGY COST: $33.25, ...",
 "qrCodeText":"https://portal.rechargealaska.net/receipt-search?session=se_54kAMdzjSULqf6TDfeu83"}
```

Nothing in our database currently references an `se_` session ID. That is why
reconciling our numbers against LynkWell's means matching two exports on charger,
clock and kilowatt-hours instead of joining on a key — and why the two double
charges above took four weeks to find. With `FinalCost` flowing, month-end
becomes a join.

**Request:** enable `DataTransfer` on the webhook feed for all our chargers. It
evidently worked on 6 January.

---

## 4. Forward the CSMS → charger direction

Every one of our 168,784 stored events is `message_type = 'CALL'` from the
charger. Six actions, nothing else: Heartbeat, StatusNotification, Authorize,
BootNotification, StartTransaction, StopTransaction. We have **zero**
`RemoteStartTransaction` records.

That is the practical limit on catching double charges ourselves. We now alert
when a card settles on a session where an app credential was stranded moments
earlier — that catches the 12 Aug shape. It cannot catch 11 Aug, because on our
side that session is just a reboot followed by a card tap.

**Request:** forward the outbound direction, or at minimum
`RemoteStartTransaction`.

---

## 5. Include `idTagInfo.status` on `Authorize`

We store only the CALL side, so an `Authorize` reaches us without its result. We
cannot tell an accepted credential from a rejected one, which is why an app
authorisation followed by a terminal-tagged start is ambiguous on our end.

**Request:** include `idTagInfo.status` (Accepted / Invalid / Blocked) on
forwarded `Authorize` events.

---

## 6. Offline authorisation of unknown cards

`04AE179C4F6181` is registered to no driver in LynkWell. On 2026-06-28 it started
two transactions at CL-D and both ended `reason: "DeAuthorized"` — the charger
was offline, authorised the card locally, and LynkWell cut it off on reconnect.
By then **23.6 kWh** had been delivered with no payer behind it.

We see rejected taps of the same shape at CL-D as recently as 6 September
(`02049F24D46000`, `08648427`), which correctly never started anything.

**Question:** what is the intended behaviour for an unrecognised card while a
charger is offline, and is local authorisation configurable per site?

---

## 7. Confirm the billing basis (low priority)

Is `Usage (kWh)` on the session export `meterStop − meterStart` from
`StopTransaction`?

Every session we have compared is consistent with it — LynkWell's energy is
greater than or equal to ours on all 445 matched August sessions and never once
lower, and the two agree exactly wherever the charger samples at transaction open
and close. But that is inference. A one-line confirmation lets us stop treating
it as an assumption.
