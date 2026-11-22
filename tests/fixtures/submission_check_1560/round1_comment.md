<!-- submission-check round=1 ai=1 kind=issue author=reporter -->
### Submission check (1 of 2)

Thanks for this. A few things would let the maintainer pick it up:

**Missing**
- Please use the [bug or feature form](https://github.com/gunkl/ClimateAdvisor/issues/new/choose), or add its parts here: what the person at home experienced, what you expected, whether you have data (attach it) or a hypothesis, and your Climate Advisor and Home Assistant versions.
- You describe the real incidents well (Nov 12 heat→cool at 7 °C, the 06:35 undertemp incident), but please state plainly what the people at home felt, e.g. how cold the house got and for how long, before the mechanism.
- Please attach the diagnostics and logs for Nov 12 and Nov 13 (with time zone). The code reading (decide_comfort_family, the unknown-family fallback) is currently a hypothesis, so please label it as one or say it is backed by the logs.
- The Nov 12 case looks like a possible bug (cooling armed 0.5 °C under the sleep ceiling in a heating season) and the Nov 13 one a post-dropout fallback problem. Please say whether you want these fixed as bugs or want the new lockout option, since a feature needs its approach agreed in the issue first.

**Suggestions**
- Consider splitting the proposal: the lockout thresholds, the HVAC season select, and the Status/briefing/Activity Record surfacing are three separable pieces, and the first is probably enough to settle first.
- Say what data would confirm the fix works, e.g. which of your logged days should show no heat→cool switch below the lockout temperature, and how you want the lockout to behave when outdoor temperature is unavailable.

<sub>An automatic check ([how it works](https://github.com/gunkl/ClimateAdvisor/blob/main/CONTRIBUTING.md#the-automatic-submission-check)). It checks once more after your next edit, push or reply, then stops.</sub>
