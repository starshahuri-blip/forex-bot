# Round 5 pre-registration (locked before any Round 5 data is downloaded)

Registered: 2026-11-15, by Claude, for the team (Star, Claude, ChatGPT GPT-6, JIM/Muse).
The git commit containing this file is the timestamp. Any change after it must be a new commit
with a reason, made before results exist, and logged in the ledger.

Trial counter before Round 5: 696. Round 5 adds 9 (5 confirmatory + 2 exploratory counted double) = 705.

## Data (all free)
- Prices: OANDA daily bid/ask candles closing 10:00 New York (liquid hours), Jan 2012 to now.
  Universe: EUR, GBP, JPY, AUD, CAD, CHF, NZD, SEK, NOK against USD.
- Interest rates: OECD 3-month interbank rates from FRED, series IR3TIB01{CC}M156N for
  US, EZ, GB, JP, AU, CA, CH, NZ, SE, NO. A currency whose series ends more than 4 months ago is
  excluded and the exclusion is printed. No rate value is ever filled in or invented.
- Cross-asset: FRED DCOILWTICO (WTI oil) and VIXCLS (VIX), daily.

## No look-ahead
- Rates: decisions at the end of month m use month m-1's value (month m is not published yet).
- Execution: signals use the close of the last day of the period; trades enter at the NEXT
  daily close and exit at the next period's equivalent point.
- Cross-asset: a signal on day D uses FRED values from D-1 and earlier.

## Costs (applied to every leg)
- OANDA bid/ask at entry and exit, plus 0.5 pip slippage per side.
- Interest carry: (foreign rate - US rate) for long foreign, the negative for short, pro rata.
- OANDA financing mark-up per leg: BASE 1.5%/yr, ADVERSE 2.5%/yr. Verdicts use ADVERSE.

## Tests
Confirmatory (can earn a forward practice test):
1. R5-1 CARRY: monthly; long the 3 highest-rate currencies vs USD, short the 3 lowest.
2. R5-2 VOL-MANAGED CARRY: R5-1 scaled by min(2, 8% / its own trailing 12-month volatility).
3. R5-3 DOLLAR CARRY: monthly; long all 9 vs USD if their average rate exceeds the US rate, else short all 9.
4. R5-4 POLICY DIVERGENCE: monthly; long the 3 currencies whose rate gap to the US widened most
   over the last 3 months, short the 3 that narrowed most.
5. R5-5 WEEKLY REVERSAL: weekly; long last week's 3 worst currencies vs USD, short the 3 best.

Exploratory (counted double; a pass only earns a NEW pre-registered test on future data):
6. R5-6 OIL -> CAD, NOK: weekly; if WTI rose over the last week, long CAD and NOK vs USD, else short.
7. R5-7 VIX -> JPY, CHF: weekly; if VIX rose over the last week, long JPY and CHF vs USD, else short.

## Pass bar (all required, under ADVERSE costs)
- t-statistic >= 3.0 using the SMALLER of the plain and Newey-West (Bartlett) t.
- Positive over the most recent 12 months.
- Positive in at least 70% of calendar years.

## Not in Round 5, and why
- Adaptive trade management and execution-cost optimisation: they amplify an existing gross edge
  and cannot create one; candle data also cannot model limit-order fills. Parked until a test survives.
- Post-hoc exploration of failed Round 4 tests (e.g. VOL-2, FIX-6): allowed only as labelled
  diagnostics. Any rule derived from them must be registered as a new test and counted.
