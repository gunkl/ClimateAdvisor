<!-- submission-check round=2 ai=1 kind=issue author=reporter -->
### Submission check (2 of 2)

Thanks for this. A few things would let the maintainer pick it up:

**Missing**
- Please use the [bug or feature form](https://github.com/gunkl/ClimateAdvisor/issues/new/choose), or add its parts here: what the person at home experienced, what you expected, whether you have data (attach it) or a hypothesis, and your Climate Advisor and Home Assistant versions.
- This issue now bundles two bugs (cooling armed on a cold night; unknown-family fallback picking cool after a dropout) and a feature (cool lockout). Please split Bugs A and B into their own issues, as you offered, so each can be diagnosed and verified on its own.
- The CA-side evidence for the Nov 12 and 13 nights is only quoted, not attached; the Activity Record rows have aged out. Please attach climate_advisor_chart_log.json, which you say still covers those nights, so the decision inputs (family state, margins, outdoor reading) can be checked.
- The Nov 13 account conflicts: the original post says the Nest rejected cool and stayed in heat, and your comment says no comfort impact. Since the Nest stayed in heat, please say whether Nov 13 is actually part of Bug B, or just a request that was rejected.

**Suggestions**
- Bug A depends on Nov 12 indoor 21.5 °C being inside the cool arming margin of a 22 °C ceiling. Please include the configured arming margin and setpoints, so we can tell whether CA behaved as designed or whether the comfort-edge selector has a defect.
- The Nov 14 reconnect followed by cool 41 ms later is your strongest evidence for Bug B. Please attach the HA log lines for 02:09 that night if you still have them. They would show whether CA acted on a stale or unknown family.
- For the lockout, please say what should happen if cool is already active when outdoor drops below the threshold: switch back to heat immediately, or wait for the next decision? That would settle the hysteresis and the capability-mask behaviour.

<sub>An automatic check ([how it works](https://github.com/gunkl/ClimateAdvisor/blob/main/CONTRIBUTING.md#the-automatic-submission-check)). That was its last automatic check.</sub>
