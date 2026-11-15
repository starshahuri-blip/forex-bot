# Round 5 results (run 2026-11-15 11:15 UTC on the droplet, code 8121320, rules ba87644)

Verdict: 0 of 7 survived. Trial counter: 705 (exploratory tests counted double). Validated strategies: 0.

| Test | Before costs | After ADVERSE costs | Verdict |
|---|---|---|---|
| R5-1 Carry | +28.4 bps/mo, t=1.7 | -29.4, t=-1.8 | Not found |
| R5-2 Vol-managed carry | +19.7, t=1.0 | -46.8, t=-2.3 | Not found |
| R5-3 Dollar carry | +7.2, t=0.4 | -21.5, t=-1.3 | Not found |
| R5-4 Policy divergence | +16.3, t=1.2 | -42.0, t=-3.2 | Not found |
| R5-5 Weekly reversal | +2.0 bps/wk, t=0.5 | -14.9, t=-3.9 | Not found |
| R5-6 Oil -> CAD, NOK (exploratory) | -0.6, t=-0.1 | -9.0, t=-2.2 | Not found |
| R5-7 VIX -> JPY, CHF (exploratory) | -10.5, t=-2.7 | -18.1, t=-4.6 | Not found |

Deviations and limits, reported as found:
- EUR and GBP were excluded by the pre-registered staleness rule: their OECD series end 2026-01
  (more than 4 months old). Universe was 7 currencies, so carry/divergence/reversal held 2 long
  and 2 short (7 // 3). The R5-3 label printed "all 9" but used the 7. No rule was changed.
- OECD 3-month interbank rates stand in for OANDA's own financing rates (no history published).

Robustness of the carry verdict to the financing assumption: costs before mark-up were about
16 bps/month (gross 28.4 minus BASE -12.7 minus the 25 bps BASE mark-up). Even with ZERO mark-up,
carry nets about +12 bps/month at t of about 0.7, far below the bar. The verdict does not depend on
the mark-up assumption.

Not allowed: reversing R5-7 after seeing it was negative. That would be a new, post-hoc test; and
even before costs it would be +10.5 bps/week at t=2.7, below the bar, with about 7.5 bps/week of
costs still to pay.
