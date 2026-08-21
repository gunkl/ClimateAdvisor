# r/homeassistant draft — "Show and Tell" flair

**Where to post:** https://reddit.com/r/homeassistant → New Post → flair "Show and Tell"

**Suggested title:**
Climate Advisor — weather + occupancy aware HVAC automation with a self-learning thermal model (now in HACS default store)

**Suggested body:**

---

Built a custom integration over the past several months that automates HVAC based on weather forecast, occupancy, and door/window state, and learns your house's actual thermal behavior instead of using fixed rate-of-change assumptions.

**Highlights:**

- Classifies each day (hot/warm/mild/cool/cold) from the forecast and picks a strategy — pre-cool before a hot day, natural ventilation on mild days, pre-heat before a cold snap
- Occupancy-aware setbacks (home/away/vacation/guest)
- Whole-house fan + HVAC fan-only ventilation support, with an economizer strategy (AC for the initial cool-down, free ventilation to maintain)
- Physics-based thermal model built from your actual HVAC/ventilation/solar behavior via regression — gets more accurate the longer it runs, works even without HVAC cycles (passive decay observations alone can seed it)
- Daily briefing explaining *why*, not just what — plus a dashboard with prediction-vs-actual charts
- Optional Claude-powered "AI Investigator" that cross-references the thermal model, event log, and compliance stats for anomaly detection

Now in the **HACS default store** — no custom repository step, just search "Climate Advisor" in HACS.

[screenshots: forecast_3d.png / status.png attached]

GitHub: https://github.com/gunkl/ClimateAdvisor

Happy to answer questions — especially interested in feedback from multi-zone or heat-pump-only setups since I've mainly been developing/testing against my own single-zone home.

---

**Notes for posting:**
- Attach 1-2 screenshots directly to the Reddit post (Reddit's own image uploader, not a GitHub link) — Reddit posts with native images get more engagement than link posts.
- r/homeassistant's rules generally require accounts to have some karma/age before "Show and Tell" posts land well — check current sub rules before posting.
- Keep it shorter than the forum version; Reddit rewards scannable posts.
