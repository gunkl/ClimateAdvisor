"""Daily briefing generator for Climate Advisor.

Voice rules for the conversational body:
- First person from the system: "I'll turn on the AC", not "the system will"
- Always "you" for the user, never "the homeowner"
- Cause-and-effect: explain *why* before asking the user to do something
- Short paragraphs (2-4 sentences max)
- Numerals for all temps and times
- No emoji in body text (emoji only in the structured header and learning section)
"""

from __future__ import annotations

import logging
import platform
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Any

from homeassistant.util import dt as dt_util

from .automation import (
    ComfortBand,
    _in_sleep_window,
    awake_comfort_edges,
    compute_pre_cool_target,
    hold_after_text,
    hold_sentence,
    resolve_pre_cool_modifier,
    select_comfort_band,
)
from .classifier import DayClassification
from .const import (
    CONF_VACATION_COOL,
    CONF_VACATION_HEAT,
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    ECONOMIZER_TEMP_DELTA,
    FAN_MODE_DISABLED,
    OCCUPANCY_SETBACK_MINUTES,
    WINDOWS_NEAR_MISS_F,
)
from .nat_vent_plan import (
    compute_nat_vent_plan,
    describe_close_timing,
    describe_nat_vent_cutoff_reason,
    describe_reopen_clause,
    effective_window_close,
    resolve_window_pair,
    resolve_with_fallback,
)
from .temperature import FAHRENHEIT, format_temp, format_temp_delta, free_cooling_direction_ok
from .timeline_surfaces import TimelineWindows, operations_lines, timeline_windows

_LOGGER = logging.getLogger(__name__)

# strftime format for 12-hour without leading zero (platform-dependent)
_FMT_HOUR = "%#I:%M %p" if platform.system() == "Windows" else "%-I:%M %p"

# Issue #1563: the briefing's language. ``generate_briefing()`` threads ``language`` down to every helper that
# builds text; the coordinator passes ``hass.config.language`` (the HA instance's own setting, unlike the dashboard
# frontend, which has no such signal and falls back to the browser's). Defaults to "en" everywhere so callers that
# don't pass it (tests, direct calls) are unaffected. ``_t()`` falls back to the English string for an unknown key
# or language, never raises, and formats with ``str.format`` only when kwargs are given.
_BRIEFING_STR: dict[str, dict[str, str]] = {
    "en": {
        "briefing.header_title": "\U0001f3e0 Your Home Climate Plan for Today",
        "briefing.now_prefix": "Now: ",
        "briefing.todays_steps": "\U0001f4cb Today's Steps",
        "briefing.suggestions_header": "\U0001f4a1 Suggestions Based on Recent Patterns",
        "briefing.suggestions_footer": (
            "To respond to these suggestions, open the Climate Advisor dashboard and visit the AI tab."
        ),
        "briefing.verb_cool": "Cool",
        "briefing.verb_heat": "Heat",
        "briefing.hvac_bank": "{verb} at {target} until the {until} high-cost period, then {after}",
        "briefing.cool_at": "Cool at {t}",
        "briefing.heat_at": "Heat at {t}",
        "briefing.label_vacation": "Vacation",
        "briefing.label_away": "Away",
        "briefing.named_band": "{label} band {band}",
        "briefing.comfort_band": "Comfort band {band}",
        "briefing.windows_day_suffix": "(windows day)",
        "briefing.open_close_by": "Open {open} – close by {close}",
        "briefing.close_by": "Close by {close}",
        "briefing.close_by_threshold_and_open": "Close by {close} (<{threshold}) / Open {open}+",
        "briefing.close_by_threshold": "Close by {close} (<{threshold})",
        "briefing.open_onward": "Open {open} onward",
        "briefing.closed_veto_heat": "Closed for now — outdoor air would cool the house below its floor",
        "briefing.closed_veto_warm": "Closed for now — outdoor air isn't cooler than indoor yet",
        "briefing.closed_all_day": "Closed all day",
        "briefing.away_setback_held": "away setback held",
        "briefing.bedtime_at": "{t} at {when}",
        "briefing.no_setback": "No setback",
        "briefing.row_day_type": "Day Type",
        "briefing.row_hvac_mode": "HVAC Mode",
        "briefing.occ_away": "Occupancy: Away — setback active",
        "briefing.occ_vacation_deep": "Occupancy: Vacation — deep setback active",
        "briefing.occ_vacation": "Occupancy: Vacation — vacation temperatures active",
        "briefing.occ_guest": "Occupancy: Guest — comfort maintained",
        "briefing.row_windows": "Windows",
        "briefing.row_bedtime_setback": "Bedtime Setback",
        "briefing.row_tomorrow": "Tomorrow",
        "briefing.trend_sig_warmer": "Significantly warmer tomorrow (+{delta})",
        "briefing.trend_warming": "Warming trend (+{delta})",
        "briefing.trend_sig_cooler": "Significant cold front coming (-{delta})",
        "briefing.trend_cooling": "Cooling trend (-{delta})",
        "briefing.trend_stable": "Stable",
        "briefing.hot_unoccupied": "A hot day, and the house is empty, so the away settings below are what apply.",
        "briefing.hot_precool_opener": (
            "Tonight I'll pre-cool the house to about {t} while outdoor air is still cool — that banking"
            " strategy cuts energy use over the course of the day."
        ),
        "briefing.hot_hold_opener": (
            "Today's forecast calls for a high near {high} — I'll hold things at {hold} through the day."
        ),
        "briefing.hot_already_above": (
            "Outdoor's already above {t} — close up for the day now. I'll handle the AC transition."
        ),
        "briefing.hot_close_around": (
            "Once outdoor temps rise above {t}, close up for the day (around {close}) — I'll handle the AC"
            " transition. Until then, enjoy the cross-breeze."
        ),
        "briefing.hot_once_closed": (
            "Once closed, keep blinds drawn on sun-facing windows (especially west-facing after noon)."
            " I'll hold things at {hold}{tail}"
        ),
        "briefing.tail_period": ".",
        "briefing.tail_rest_of_day": " for the rest of the day.",
        "briefing.hot_sealed_blinds_hold": (
            "Today's a keep-it-sealed kind of day. Close the blinds on sun-facing windows (especially"
            " west-facing ones after noon) and I'll hold things at {hold}."
        ),
        "briefing.hot_sealed_blinds_handle": (
            "Today's a keep-it-sealed kind of day. Close the blinds on sun-facing windows (especially"
            " west-facing ones after noon) and I'll handle the rest at {hold}."
        ),
        "briefing.hot_fan_helps": (
            "When ventilation windows open, I'll use the fan to help pull that cool outdoor air through the house."
        ),
        "briefing.warm_close_now": "Close up now — outdoor's already warmed past indoor.",
        "briefing.at_prep": "at ",
        "briefing.warm_close_at": "Close up {prep}{close_phrase} {reason}.",
        "briefing.warm_windows_open_morning": (
            "Windows are open to catch the cool morning air — cross-ventilation keeps things comfortable"
            " without the AC."
        ),
        "briefing.warm_unoccupied": "A warm day, and the house is empty, so the away settings below are what apply.",
        "briefing.warm_heater_ran_overnight": (
            "The heater ran a bit overnight before settling — the thermostat is holding {band} today."
        ),
        "briefing.warm_air_wont_help": "Outside air won't do much for the house today, so the thermostat holds {band}.",
        "briefing.warm_holds_band": "The thermostat holds {band} today.",
        "briefing.warm_fan_boost": "I'll use the fan to boost cross-ventilation when windows are open.",
        "briefing.ac_auto_tail_windows": (
            " Once windows are closed, the AC will step in automatically if it's needed to hold that ceiling."
        ),
        "briefing.ac_auto_tail": " The AC will step in automatically if it's needed to hold that ceiling.",
        "briefing.indoor_forecast_reach": "Indoor temps are forecast to reach {hold} around {t}.{tail}",
        "briefing.warm_close_before_climb": (
            "Close up by {close} before outdoor temps climb — seal the cool air inside so the AC can take"
            " over above {hold}."
        ),
        "briefing.warm_ac_safety_net": (
            "The AC will step in above {hold} as a safety net if needed, but with good airflow you probably"
            " won't need it."
        ),
        "briefing.warm_ac_steps_in": "The AC will step in above {hold} to hold your comfort ceiling.",
        "briefing.warm_precool_tonight": (
            "Tonight I'll cool the home to {t} while you sleep to build up cold thermal mass before tomorrow's"
            " heat — so the house coasts longer before the AC needs to kick in."
        ),
        "briefing.when_evening_cools": "when the evening air cools back down",
        "briefing.reopen_turn_off_ac": "Reopen windows around {t} {reason} — I'll turn off the AC.",
        "briefing.reopen_around": "Reopen windows around {t} {reason}.",
        "briefing.thermostat_holds_lc": "the thermostat holds {band}",
        "briefing.mild_unoccupied": "A mild day, and the house is empty, so the away settings below are what apply.",
        "briefing.mild_air_wont_help": (
            "A mild day. Outside air isn't going to warm the house, so {holding} and steps in if it drifts."
        ),
        "briefing.mild_default": "A mild day, so {holding} and steps in if it drifts.",
        "briefing.mild_near_miss": (
            "The forecast high of {high} is just under your {floor} floor and forecasts can run low, so"
            " outside air may help later this afternoon. I'll keep going on the thermostat until it does."
        ),
        "briefing.mild_warmed_before_sunrise": (
            "A day where the house practically takes care of itself. I warmed to {t} before sunrise —"
            " now the weather does the rest and {holding}."
        ),
        "briefing.mild_held_on_own": (
            "A day where the house practically takes care of itself — it held at or above {t} on its own"
            " overnight, so HVAC stayed off and the weather did the rest."
        ),
        "briefing.mild_open_south_east": (
            "Open south and east windows for a natural cross-breeze that freshens the air and warms the house"
            " for free."
        ),
        "briefing.mild_close_now": (
            "Close up now — outdoor's already warmed past indoor. If the house dips below {t} tonight,"
            " the heater comes back on automatically."
        ),
        "briefing.trap_the_warmth": "to trap the warmth",
        "briefing.by_prep": "by ",
        "briefing.mild_close_by": (
            "Close up {prep}{close} {reason}. If the house dips below {t} tonight, the heater comes back on"
            " automatically."
        ),
        "briefing.cool_unoccupied": "A cool day, and the house is empty, so the away settings below are what apply.",
        "briefing.cool_heater_day": "Heater day — too cool outside for windows. I'll hold {t} through the day.",
        "briefing.cool_bedtime_setback": (
            "At bedtime I'll set back to {t} — most people sleep better a little cooler."
        ),
        "briefing.cold_unoccupied": "A cold day, and the house is empty, so the away settings below are what apply.",
        "briefing.cold_day_help": (
            "Cold day — heater runs all day. Help it out: close north-side curtains, open south-facing ones"
            " for free solar heat, and minimize time holding exterior doors open."
        ),
        "briefing.cold_tomorrow_colder": "Tomorrow's even colder, so expect the heater to work harder in the morning.",
        "briefing.cold_bedtime_setback": "At bedtime I'll set back to {t}.",
        "briefing.cold_conservative_setback": (
            "Tonight I'm using a conservative setback — {t} instead of the usual {usual}. When it's this"
            " cold, a deeper setback takes too long to recover from in the morning."
        ),
        "briefing.vacation_band": (
            "While you're on vacation, I'm keeping the house at your vacation temperatures ({band})."
            " Comfort temperatures will be restored when you return."
        ),
        "briefing.vacation_deeper": (
            "While you're on vacation, I'm keeping the house at a deeper energy-saving setback to save energy."
            " Comfort temperatures will be restored when you return."
        ),
        "briefing.guest_visiting": (
            "Guests are visiting — maintaining full comfort temperatures. Away setbacks are disabled while"
            " guest mode is active."
        ),
        "briefing.away_cool_overridden": (
            "You're currently away — setback should be in effect (target {t}). Comfort will be restored"
            " when you return — give it 20 to 30 minutes to feel normal again."
        ),
        "briefing.away_cool_applied": (
            "You're currently away. I've applied setback temperatures, letting the house drift up to {t} to"
            " save energy. Comfort will be restored when you return — give it 20 to 30 minutes to feel"
            " normal again."
        ),
        "briefing.away_heat_overridden": (
            "You're currently away — setback should be in effect (target {t}). Comfort will be restored"
            " when you return — should take 20 to 30 minutes depending on how long you've been gone."
        ),
        "briefing.away_heat_applied": (
            "You're currently away. I've dropped to {t} to save energy. Comfort will be restored when you"
            " return — should take 20 to 30 minutes depending on how long you've been gone."
        ),
        "briefing.away_band": (
            "You're currently away. I'm holding the wider energy-saving band ({band}) while you're out, so"
            " not much should run. Comfort will be restored when you return."
        ),
        "briefing.home_leave_cool": (
            "If you head out, no worries. After about {minutes} minutes I'll let the house drift up to {t} to"
            " save energy. When you're back, I'll pull it right back down — give it 20 to 30 minutes to"
            " feel normal again."
        ),
        "briefing.home_leave_heat": (
            "If you head out, I'll drop to {t} after about {minutes} minutes. When you get back, I'll warm"
            " things right up — should take 20 to 30 minutes depending on how long you were gone."
        ),
        "briefing.home_leave_band": (
            "If you head out, not much changes today — after about {minutes} minutes I'll widen the band"
            " to {band} to save energy, and restore comfort when you're back."
        ),
        "briefing.minute": "{n} minute",
        "briefing.minutes": "{n} minutes",
        "briefing.fresh_hold_target": " to hold your {t} target",
        "briefing.fresh_natvent_active": (
            "Windows are open and outdoor air is {t} — I'll use the fan{target} without running the AC."
            " Once you close up, I'll resume normal cooling."
        ),
        "briefing.fresh_pull_back": (
            " Just know that on a day like today it may take a bit longer to pull back down to {t}, so if you"
            " want to minimize the impact, shorter is better — and try to keep other windows and doors"
            " shut while you've got one open."
        ),
        "briefing.fresh_cool": (
            "If you want to crack a window for some fresh air, no problem — it's your house. I'll keep"
            " the AC running for a bit in case it's just a quick thing, but if it stays open past {debounce}"
            " I'll shut the AC off so you're not cooling the outdoors. Once you close up, I'll fire the AC"
            " back up right away.{pull_back}"
        ),
        "briefing.fresh_heat": (
            "If you want to open a window for some fresh air, no problem — go for it. I'll keep the heat"
            " running for a bit in case you're just airing things out, but if it stays open past {debounce}"
            " I'll turn the heat off so we're not heating the neighborhood. Once you close up, the heat kicks"
            " right back on. It'll take a little extra energy to warm back up, so if you want to minimize the"
            " impact, a quick burst of fresh air works great — and closing doors to the room with the"
            " open window helps keep the rest of the house comfortable while you do it."
        ),
        "briefing.fresh_holding_clause": " — the thermostat is holding {band} today",
        "briefing.fresh_off": (
            "If you want to open a window for some fresh air, go for it{holding}. Enjoy the breeze. If the"
            " heat or AC does need to run while a window is still open, I'll give it {debounce} and then"
            " pause until you close up."
        ),
        "briefing.grace_manual": (
            "One heads-up: you manually turned the HVAC back on earlier, so I'm in a {grace} hands-off window"
            " right now. During that window, opening a door or window won't trigger a pause — I'm giving"
            " you space to settle in without the system jumping in. Once the window closes, door/window"
            " sensing goes back to normal."
        ),
        "briefing.grace_automation": (
            "One heads-up: I just resumed the HVAC after all the doors and windows closed, so I'm in a"
            " {grace} settling period. During that time, opening a door or window briefly won't immediately"
            " pause things again — this prevents the system from cycling on and off if you're moving in"
            " and out. After the settling period, normal door/window sensing resumes."
        ),
        "briefing.tonight_warmer_away": "Looking ahead — tomorrow's warmer at {t}.",
        "briefing.tonight_warmer_home": (
            "Looking ahead — tomorrow's warmer at {t}, so I'm going to set back a bit more aggressively"
            " tonight. Less heating needed means energy saved while you sleep."
        ),
        "briefing.tonight_cooler": (
            "Looking ahead — tomorrow's cooler at {t}, so expect the heater to run more tomorrow."
        ),
        "briefing.tonight_similar": (
            "Tomorrow looks pretty similar to today — {t} for a high. Nothing special planned overnight."
        ),
        "briefing.tonight_adaptive_tuned": (
            "Bedtime setback and pre-heat timing are tuned to your home's actual heating performance."
        ),
    },
    "fr": {
        "briefing.header_title": "\U0001f3e0 Votre plan climatique du jour",
        "briefing.now_prefix": "Maintenant : ",
        "briefing.todays_steps": "\U0001f4cb Étapes du jour",
        "briefing.suggestions_header": "\U0001f4a1 Suggestions basées sur les habitudes récentes",
        "briefing.suggestions_footer": (
            "Pour répondre à ces suggestions, ouvrez le tableau de bord Climate Advisor"
            " et allez dans l'onglet IA."
        ),
        "briefing.verb_cool": "Climatise",
        "briefing.verb_heat": "Chauffe",
        "briefing.hvac_bank": "{verb} à {target} jusqu'à la période de pointe de {until}, puis {after}",
        "briefing.cool_at": "Climatise à {t}",
        "briefing.heat_at": "Chauffe à {t}",
        "briefing.label_vacation": "Vacances",
        "briefing.label_away": "Absence",
        "briefing.named_band": "Plage {label} {band}",
        "briefing.comfort_band": "Plage de confort {band}",
        "briefing.windows_day_suffix": "(jour fenêtres)",
        "briefing.open_close_by": "Ouvre {open} – ferme avant {close}",
        "briefing.close_by": "Ferme avant {close}",
        "briefing.close_by_threshold_and_open": "Ferme avant {close} (<{threshold}) / Ouvre {open}+",
        "briefing.close_by_threshold": "Ferme avant {close} (<{threshold})",
        "briefing.open_onward": "Ouvre à partir de {open}",
        "briefing.closed_veto_heat": (
            "Fermées pour l'instant — l'air extérieur refroidirait la maison sous son plancher"
        ),
        "briefing.closed_veto_warm": (
            "Fermées pour l'instant — l'air extérieur n'est pas encore plus frais que l'intérieur"
        ),
        "briefing.closed_all_day": "Fermées toute la journée",
        "briefing.away_setback_held": "repli absence maintenu",
        "briefing.bedtime_at": "{t} à {when}",
        "briefing.no_setback": "Pas de repli",
        "briefing.row_day_type": "Type de journée",
        "briefing.row_hvac_mode": "Mode CVC",
        "briefing.occ_away": "Occupation : Absent — repli actif",
        "briefing.occ_vacation_deep": "Occupation : Vacances — repli profond actif",
        "briefing.occ_vacation": "Occupation : Vacances — températures vacances actives",
        "briefing.occ_guest": "Occupation : Invité — confort maintenu",
        "briefing.row_windows": "Fenêtres",
        "briefing.row_bedtime_setback": "Repli nocturne",
        "briefing.row_tomorrow": "Demain",
        "briefing.trend_sig_warmer": "Nettement plus chaud demain (+{delta})",
        "briefing.trend_warming": "Tendance au réchauffement (+{delta})",
        "briefing.trend_sig_cooler": "Front froid important à venir (-{delta})",
        "briefing.trend_cooling": "Tendance au refroidissement (-{delta})",
        "briefing.trend_stable": "Stable",
        "briefing.hot_unoccupied": (
            "Une journée chaude, et la maison est vide, donc ce sont les réglages d'absence"
            " ci-dessous qui s'appliquent."
        ),
        "briefing.hot_precool_opener": (
            "Ce soir, je vais pré-rafraîchir la maison à environ {t} pendant que l'air extérieur"
            " est encore frais — cette stratégie réduit la consommation d'énergie sur"
            " l'ensemble de la journée."
        ),
        "briefing.hot_hold_opener": (
            "Les prévisions annoncent un maximum proche de {high} — je vais maintenir {hold} toute"
            " la journée."
        ),
        "briefing.hot_already_above": (
            "Il fait déjà plus de {t} dehors — fermez dès maintenant pour la journée."
            " Je gère la transition vers la clim."
        ),
        "briefing.hot_close_around": (
            "Dès que la température extérieure dépasse {t}, fermez pour la journée"
            " (vers {close}) — je gère la transition vers la clim. D'ici là, profitez du"
            " courant d'air traversant."
        ),
        "briefing.hot_once_closed": (
            "Une fois fermé, gardez les stores baissés sur les fenêtres exposées au soleil"
            " (surtout côté ouest l'après-midi). Je vais maintenir {hold}{tail}"
        ),
        "briefing.tail_period": ".",
        "briefing.tail_rest_of_day": " pour le reste de la journée.",
        "briefing.hot_sealed_blinds_hold": (
            "Aujourd'hui, mieux vaut tout garder fermé. Baissez les stores sur les fenêtres exposées"
            " au soleil (surtout côté ouest l'après-midi) et je vais maintenir {hold}."
        ),
        "briefing.hot_sealed_blinds_handle": (
            "Aujourd'hui, mieux vaut tout garder fermé. Baissez les stores sur les fenêtres exposées"
            " au soleil (surtout côté ouest l'après-midi) et je m'occupe du reste à {hold}."
        ),
        "briefing.hot_fan_helps": (
            "Quand les fenêtres de ventilation s'ouvrent, j'utilise le ventilateur pour aider à faire"
            " circuler cet air frais extérieur dans la maison."
        ),
        "briefing.warm_close_now": "Fermez maintenant — l'air extérieur a déjà dépassé la température intérieure.",
        "briefing.at_prep": "à ",
        "briefing.warm_close_at": "Fermez {prep}{close_phrase} {reason}.",
        "briefing.warm_windows_open_morning": (
            "Les fenêtres sont ouvertes pour profiter de l'air frais du matin — la ventilation"
            " traversante garde la maison confortable sans la clim."
        ),
        "briefing.warm_unoccupied": (
            "Une journée douce, et la maison est vide, donc ce sont les réglages d'absence ci-dessous"
            " qui s'appliquent."
        ),
        "briefing.warm_heater_ran_overnight": (
            "Le chauffage a tourné un peu cette nuit avant de se stabiliser — le thermostat maintient"
            " {band} aujourd'hui."
        ),
        "briefing.warm_air_wont_help": (
            "L'air extérieur n'apportera pas grand-chose à la maison aujourd'hui, donc le thermostat"
            " maintient {band}."
        ),
        "briefing.warm_holds_band": "Le thermostat maintient {band} aujourd'hui.",
        "briefing.warm_fan_boost": (
            "J'utilise le ventilateur pour renforcer la ventilation traversante quand les fenêtres sont"
            " ouvertes."
        ),
        "briefing.ac_auto_tail_windows": (
            " Une fois les fenêtres fermées, la clim se déclenchera automatiquement si nécessaire"
            " pour maintenir ce plafond."
        ),
        "briefing.ac_auto_tail": (
            " La clim se déclenchera automatiquement si nécessaire pour maintenir ce plafond."
        ),
        "briefing.indoor_forecast_reach": (
            "La température intérieure devrait atteindre {hold} vers {t}.{tail}"
        ),
        "briefing.warm_close_before_climb": (
            "Fermez avant {close}, avant que la température extérieure ne grimpe — gardez l'air"
            " frais à l'intérieur pour que la clim puisse prendre le relais au-dessus de {hold}."
        ),
        "briefing.warm_ac_safety_net": (
            "La clim se déclenchera au-dessus de {hold} comme filet de sécurité si besoin, mais"
            " avec une bonne circulation d'air vous n'en aurez probablement pas besoin."
        ),
        "briefing.warm_ac_steps_in": (
            "La clim se déclenchera au-dessus de {hold} pour maintenir votre plafond de confort."
        ),
        "briefing.warm_precool_tonight": (
            "Ce soir, je vais rafraîchir la maison à {t} pendant votre sommeil pour accumuler de la"
            " fraîcheur avant la chaleur de demain — la maison tiendra ainsi plus longtemps avant que"
            " la clim doive s'enclencher."
        ),
        "briefing.when_evening_cools": "quand l'air du soir redescend",
        "briefing.reopen_turn_off_ac": "Rouvrez les fenêtres vers {t} {reason} — j'éteins la clim.",
        "briefing.reopen_around": "Rouvrez les fenêtres vers {t} {reason}.",
        "briefing.thermostat_holds_lc": "le thermostat maintient {band}",
        "briefing.mild_unoccupied": (
            "Une journée douce, et la maison est vide, donc ce sont les réglages d'absence ci-dessous"
            " qui s'appliquent."
        ),
        "briefing.mild_air_wont_help": (
            "Une journée douce. L'air extérieur ne va pas réchauffer la maison, donc {holding}"
            " et intervient si elle dérive."
        ),
        "briefing.mild_default": "Une journée douce, donc {holding} et intervient si elle dérive.",
        "briefing.mild_near_miss": (
            "Le maximum prévu de {high} est juste en dessous de votre plancher de {floor} et les"
            " prévisions peuvent être sous-estimées, donc l'air extérieur pourrait aider plus"
            " tard dans l'après-midi. Je continue sur le thermostat en attendant."
        ),
        "briefing.mild_warmed_before_sunrise": (
            "Une journée où la maison se débrouille presque toute seule. J'ai chauffé à"
            " {t} avant le lever du soleil — maintenant la météo fait le reste et {holding}."
        ),
        "briefing.mild_held_on_own": (
            "Une journée où la maison se débrouille presque toute seule — elle s'est maintenue"
            " à {t} ou plus toute seule cette nuit, donc le chauffage est resté éteint et la"
            " météo a fait le reste."
        ),
        "briefing.mild_open_south_east": (
            "Ouvrez les fenêtres côté sud et est pour une ventilation naturelle qui rafraîchit"
            " l'air et réchauffe la maison gratuitement."
        ),
        "briefing.mild_close_now": (
            "Fermez maintenant — l'air extérieur a déjà dépassé la température"
            " intérieure. Si la maison descend sous {t} cette nuit, le chauffage se rallume"
            " automatiquement."
        ),
        "briefing.trap_the_warmth": "pour garder la chaleur",
        "briefing.by_prep": "avant ",
        "briefing.mild_close_by": (
            "Fermez {prep}{close} {reason}. Si la maison descend sous {t} cette nuit, le chauffage se rallume"
            " automatiquement."
        ),
        "briefing.cool_unoccupied": (
            "Une journée fraîche, et la maison est vide, donc ce sont les réglages d'absence"
            " ci-dessous qui s'appliquent."
        ),
        "briefing.cool_heater_day": (
            "Journée de chauffage — trop frais dehors pour les fenêtres. Je vais maintenir {t}"
            " toute la journée."
        ),
        "briefing.cool_bedtime_setback": (
            "Au coucher, je vais réduire à {t} — on dort généralement mieux dans une"
            " pièce un peu plus fraîche."
        ),
        "briefing.cold_unoccupied": (
            "Une journée froide, et la maison est vide, donc ce sont les réglages d'absence ci-dessous"
            " qui s'appliquent."
        ),
        "briefing.cold_day_help": (
            "Journée froide — le chauffage tourne toute la journée. Aidez-le : fermez les"
            " rideaux côté nord, ouvrez ceux côté sud pour profiter du soleil gratuitement, et"
            " limitez le temps où les portes extérieures restent ouvertes."
        ),
        "briefing.cold_tomorrow_colder": (
            "Demain sera encore plus froid, attendez-vous à ce que le chauffage travaille plus le matin."
        ),
        "briefing.cold_bedtime_setback": "Au coucher, je vais réduire à {t}.",
        "briefing.cold_conservative_setback": (
            "Ce soir, j'applique une réduction prudente — {t} au lieu des {usual} habituels. Par ce"
            " froid, une réduction plus profonde met trop de temps à récupérer le matin."
        ),
        "briefing.vacation_band": (
            "Pendant vos vacances, je maintiens la maison à vos températures de vacances ({band})."
            " Les températures de confort seront rétablies à votre retour."
        ),
        "briefing.vacation_deeper": (
            "Pendant vos vacances, je maintiens la maison avec une réduction d'énergie plus profonde."
            " Les températures de confort seront rétablies à votre retour."
        ),
        "briefing.guest_visiting": (
            "Vous avez des invités — je maintiens les températures de confort complètes. Les"
            " réductions d'absence sont désactivées tant que le mode invité est actif."
        ),
        "briefing.away_cool_overridden": (
            "Vous êtes actuellement absent — la réduction devrait être active (cible {t})."
            " Le confort sera rétabli à votre retour — comptez 20 à 30 minutes pour que ça"
            " redevienne normal."
        ),
        "briefing.away_cool_applied": (
            "Vous êtes actuellement absent. J'ai appliqué les températures de réduction,"
            " laissant la maison monter jusqu'à {t} pour économiser l'énergie. Le confort sera"
            " rétabli à votre retour — comptez 20 à 30 minutes pour que ça redevienne"
            " normal."
        ),
        "briefing.away_heat_overridden": (
            "Vous êtes actuellement absent — la réduction devrait être active (cible {t})."
            " Le confort sera rétabli à votre retour — ça prendra 20 à 30 minutes selon"
            " la durée de votre absence."
        ),
        "briefing.away_heat_applied": (
            "Vous êtes actuellement absent. J'ai baissé à {t} pour économiser l'énergie."
            " Le confort sera rétabli à votre retour — ça prendra 20 à 30 minutes selon"
            " la durée de votre absence."
        ),
        "briefing.away_band": (
            "Vous êtes actuellement absent. Je maintiens la plage élargie d'économie d'énergie"
            " ({band}) pendant votre absence, donc peu de choses devraient fonctionner. Le confort sera"
            " rétabli à votre retour."
        ),
        "briefing.home_leave_cool": (
            "Si vous sortez, pas d'inquiétude. Après environ {minutes} minutes, je laisse la maison"
            " monter jusqu'à {t} pour économiser l'énergie. À votre retour, je la redescends"
            " tout de suite — comptez 20 à 30 minutes pour que ça redevienne normal."
        ),
        "briefing.home_leave_heat": (
            "Si vous sortez, je baisse à {t} après environ {minutes} minutes. À votre retour, je"
            " réchauffe tout de suite — ça prendra 20 à 30 minutes selon la durée de"
            " votre absence."
        ),
        "briefing.home_leave_band": (
            "Si vous sortez, pas grand changement aujourd'hui — après environ {minutes} minutes,"
            " j'élargis la plage à {band} pour économiser l'énergie, et je rétablis le"
            " confort à votre retour."
        ),
        "briefing.minute": "{n} minute",
        "briefing.minutes": "{n} minutes",
        "briefing.fresh_hold_target": " pour maintenir votre cible de {t}",
        "briefing.fresh_natvent_active": (
            "Les fenêtres sont ouvertes et l'air extérieur est à {t} — j'utilise le"
            " ventilateur{target} sans faire tourner la clim. Une fois les fenêtres fermées, je"
            " reprends le rafraîchissement normal."
        ),
        "briefing.fresh_pull_back": (
            " Sachez qu'un jour comme aujourd'hui, redescendre à {t} peut prendre un peu plus de temps,"
            " donc si vous voulez limiter l'impact, mieux vaut faire court — et essayez de garder les"
            " autres fenêtres et portes fermées pendant ce temps."
        ),
        "briefing.fresh_cool": (
            "Si vous voulez entrouvrir une fenêtre pour prendre l'air, aucun problème — c'est"
            " chez vous. Je laisse la clim tourner un peu au cas où ce soit rapide, mais si ça reste"
            " ouvert au-delà de {debounce}, je coupe la clim pour ne pas refroidir l'extérieur. Une"
            " fois refermé, je relance la clim tout de suite.{pull_back}"
        ),
        "briefing.fresh_heat": (
            "Si vous voulez ouvrir une fenêtre pour prendre l'air, aucun problème — allez-y. Je"
            " laisse le chauffage tourner un peu au cas où vous aériez juste, mais si ça reste"
            " ouvert au-delà de {debounce}, j'éteins le chauffage pour ne pas chauffer le voisinage."
            " Une fois refermé, le chauffage se rallume aussitôt. Ça demandera un peu plus"
            " d'énergie pour remonter en température, donc si vous voulez limiter l'impact, une"
            " courte aération fonctionne très bien — et fermer la porte de la pièce avec la"
            " fenêtre ouverte aide à garder le reste de la maison confortable pendant ce temps."
        ),
        "briefing.fresh_holding_clause": " — le thermostat maintient {band} aujourd'hui",
        "briefing.fresh_off": (
            "Si vous voulez ouvrir une fenêtre pour prendre l'air, allez-y{holding}. Profitez de la"
            " brise. Si le chauffage ou la clim doit quand même tourner pendant qu'une fenêtre est"
            " encore ouverte, je lui laisse {debounce} puis je mets en pause jusqu'à ce que vous"
            " refermiez."
        ),
        "briefing.grace_manual": (
            "Petite info : vous avez remis le chauffage/la clim en marche manuellement tout à"
            " l'heure, donc je suis dans une fenêtre de non-intervention de {grace} en ce moment."
            " Pendant ce temps, ouvrir une porte ou une fenêtre ne déclenchera pas de pause — je"
            " vous laisse le temps de vous installer sans que le système n'intervienne. Une fois cette"
            " fenêtre terminée, la détection des portes et fenêtres reprend normalement."
        ),
        "briefing.grace_automation": (
            "Petite info : je viens de reprendre la main sur le chauffage/la clim après que toutes"
            " les portes et fenêtres ont été fermées, donc je suis dans une période de"
            " stabilisation de {grace}. Pendant ce temps, ouvrir brièvement une porte ou une fenêtre"
            " ne déclenchera pas immédiatement une nouvelle pause — cela évite que le"
            " système s'arrête et redémarre sans cesse si vous allez et venez. Après cette"
            " période, la détection des portes et fenêtres reprend normalement."
        ),
        "briefing.tonight_warmer_away": "En perspective — demain sera plus chaud, à {t}.",
        "briefing.tonight_warmer_home": (
            "En perspective — demain sera plus chaud, à {t}, donc je vais réduire un peu plus"
            " fort cette nuit. Moins de chauffage nécessaire veut dire de l'énergie économisée"
            " pendant votre sommeil."
        ),
        "briefing.tonight_cooler": (
            "En perspective — demain sera plus frais, à {t}, donc attendez-vous à ce que le"
            " chauffage tourne plus demain."
        ),
        "briefing.tonight_similar": (
            "Demain ressemble beaucoup à aujourd'hui — {t} de maximum. Rien de particulier prévu"
            " cette nuit."
        ),
        "briefing.tonight_adaptive_tuned": (
            "Le réglage nocturne et le pré-chauffage sont ajustés selon les performances"
            " réelles de chauffage de votre maison."
        ),
    },
}


def _t(language: str, key: str, **kwargs: Any) -> str:
    table = _BRIEFING_STR.get(language) or _BRIEFING_STR["en"]
    s = table.get(key, _BRIEFING_STR["en"].get(key, key))
    return s.format(**kwargs) if kwargs else s


@dataclass(frozen=True)
class _CloseFact:
    """The dated moment today's planned window period ends, resolved by ``effective_window_close()``.

    ``dynamic``: the forecast-derived cutoff is in play (dated today, or already behind us), so its reason may be
    stated. False means today's static configured close (a cutoff dated another day says nothing about today).
    ``already``: the close condition was already true when the plan was built -> the briefing says "now".
    """

    at: datetime
    dynamic: bool
    already: bool

    @property
    def text(self) -> str:
        """'11:00 AM', or 'now' when the close condition was already reached."""
        return describe_close_timing(self.at.strftime(_FMT_HOUR), self.already)


@dataclass(frozen=True)
class _BriefingFacts:
    """Everything the briefing says about the house, gathered ONCE at write time (Issue #1231, absorbs #1219).

    The briefing is written on demand or on the morning schedule from what is known at that moment, so every
    sentence and TLDR row reads from here -- never from its own re-derivation -- and a value that is unknown or
    not applied is simply absent (None) rather than guessed. ``unoccupied`` (away/vacation) means the engine
    holds the away/vacation setback all day and all night (``handle_bedtime`` defers to it), so nothing at-home
    -- no hold temperature, bedtime, pre-cool or window advice -- is promised.
    """

    occupancy_mode: str
    unoccupied: bool
    band: ComfortBand  # the band the thermostat is armed with right now (awake) for this occupancy
    awake_heat: float
    awake_cool: float
    normal_bedtime: float | None  # the edge the engine arms at bedtime at home (None: no setback / unoccupied)
    bedtime_target: float | None  # adaptive value when learned, else normal_bedtime (None when unoccupied)
    windows_advice: bool  # warm/mild: windows recommended today AND someone home
    near_miss: bool
    close: _CloseFact | None  # warm/mild/hot-morning planned close; None when no window advice applies
    evening_open: datetime | None  # warm/mild reopen, pair-validated against the dynamic close
    ceiling_breach: datetime | None  # warm: indoor forecast to reach the ceiling TODAY (another day -> None)
    hot_morning: bool
    hot_evening: bool
    hot_evening_open: time | None
    pre_cool_target: float | None  # warm-day "tonight I'll cool the home" target (None when unoccupied)
    hot_pre_cool_target: float | None  # hot-day overnight pre-cool target (None when unoccupied / not expected)
    economizer_threshold: float  # comfort_cool + ECONOMIZER_TEMP_DELTA: the outdoor temperature windows close at
    # Issue #1369: vacation band deeper than away on the edge that matters (heat day: floor, cool day: ceiling,
    # otherwise both edges). False when not on vacation. Separate vacation setpoints (#1347) may be equal/milder.
    vacation_deeper: bool = False
    # Issue #1230 R3b: the planned open still ahead today (warm/mild), so the Windows row names BOTH times;
    # and the live direction veto ("heat" or "warm") on a period that is running, so the row says "closed for now".
    open_at: datetime | None = None
    window_veto: str | None = None
    # Issue #1404: the time-of-use bank holding the house right now (``automation.CurrentHold`` with a bank), or None.
    current_hold: Any = None


def _local(moment: datetime, now: datetime) -> datetime:
    """``moment`` in ``now``'s timezone (both aware), unchanged otherwise."""
    if moment.tzinfo is not None and now.tzinfo is not None:
        return moment.astimezone(now.tzinfo)
    return moment


def _close_fact(cutoff: datetime | None, already: bool, static_close: time | None, now: datetime) -> _CloseFact | None:
    """Resolve a close time through the ONE shared helper (``effective_window_close()``)."""
    at = effective_window_close(cutoff, already, static_close, now)
    if at is None:
        return None
    dynamic = cutoff is not None and (already or at == cutoff)
    return _CloseFact(at=at, dynamic=dynamic, already=cutoff is not None and already)


def _gives_window_advice(c: DayClassification, occupancy_mode: str) -> bool:
    """Whether the briefing may talk about opening/closing windows today (Issue #1211).

    Only when windows are recommended for the day (outside air helps today) AND someone is home: an empty house
    (away/vacation) gets no window instructions even though the flag stays set for the automation.
    """
    # The briefing is the day's plan, so it follows the classifier's forecast-based answer. The coordinator's
    # live outdoor gate can clear ``windows_recommended`` for a few hours (a cold house on a cool morning) and
    # restore it later; the briefing text neither flaps with it nor depends on the moment it was rendered, and
    # the regeneration trigger keys on the same ``forecast_windows_recommended``. (The live direction is the
    # Next User Action card's job.) ``windows_recommended`` alone still counts, for callers that build a
    # classification by hand.
    recommended = bool(c.forecast_windows_recommended or c.windows_recommended)
    return recommended and occupancy_mode not in ("away", "vacation")


def _is_mild_near_miss(c: DayClassification, comfort_heat: float, *, unoccupied: bool, advice: bool) -> bool:
    """A mild day whose forecast high is just under the comfort floor (Issue #1211, owner decision O7).

    The forecast ran 5-10 F low on every recorded flip day, so instead of going silent the briefing says
    outside air may help later. Never on an estimated low, never away, never when windows are already advised.
    """
    return (
        c.day_type == DAY_TYPE_MILD
        and not advice
        and not unoccupied
        and not c.low_is_estimated
        and comfort_heat - WINDOWS_NEAR_MISS_F <= c.today_high < comfort_heat
    )


def _vacation_band_is_deeper(c: DayClassification, band: ComfortBand, config: dict) -> bool:
    """Issue #1369: whether the vacation band is deeper than away on the edge that matters today.

    A heat day arms only the floor and a cool day only the ceiling, so that edge decides; any other day arms both
    edges, so both must be deeper. Equal is not deeper.
    """
    floor_deeper = band.floor < float(config["setback_heat"])
    ceiling_deeper = band.ceiling > float(config["setback_cool"])
    if c.hvac_mode == "heat":
        return floor_deeper
    if c.hvac_mode == "cool":
        return ceiling_deeper
    return floor_deeper and ceiling_deeper


def _gather_briefing_facts(
    c: DayClassification,
    config: dict,
    *,
    occupancy_mode: str = "home",
    bedtime_setback_heat: float | None = None,
    bedtime_setback_cool: float | None = None,
    warm_events: dict | None = None,
    mild_events: dict | None = None,
    hot_events: dict | None = None,
    runtime_config: dict | None = None,
    now: datetime | None = None,
    awake_edges: tuple[float, float] | None = None,
    windows: TimelineWindows | None = None,
) -> _BriefingFacts:
    """Gather the briefing's facts once (Issue #1231). ``config`` is the TLDR-shape dict built by
    ``generate_briefing()`` (comfort/setback/sleep_time/wake_time/aggressive_savings)."""
    now = now if now is not None else dt_util.now()
    unoccupied = occupancy_mode in ("away", "vacation")
    savings = bool(config.get("aggressive_savings", False))
    # The engine's band selector reads sleep_heat/sleep_cool from its config; carry them from the runtime config
    # when we have it (else it falls back to its own documented defaults, exactly as the engine would).
    band_cfg = dict(config)
    # Issue #1369: also the optional separate vacation setpoints (#1347), so the vacation band is the one
    # vacation_edges() resolves for the engine rather than always away -/+ 3.
    for key in ("sleep_heat", "sleep_cool", CONF_VACATION_HEAT, CONF_VACATION_COOL):
        if runtime_config and key in runtime_config:
            band_cfg[key] = runtime_config[key]
    band = select_comfort_band(
        c, band_cfg, occupancy_mode=occupancy_mode, in_sleep_window=False, aggressive_savings=savings
    )
    vacation_deeper = occupancy_mode == "vacation" and _vacation_band_is_deeper(c, band, config)
    awake_heat, awake_cool = (
        awake_edges if awake_edges is not None else awake_comfort_edges(config, aggressive_savings=savings)
    )

    # Bedtime: the edge handle_bedtime() arms at home is the sleep band's edge on this day's side. The adaptive
    # value (when learned) wins; unoccupied there is no at-home bedtime at all (the away setback is held).
    normal_bedtime: float | None = None
    bedtime_target: float | None = None
    if not unoccupied and c.hvac_mode in ("heat", "cool"):
        asleep = select_comfort_band(
            c, band_cfg, occupancy_mode="home", in_sleep_window=True, aggressive_savings=savings
        )
        normal_bedtime = asleep.floor if c.hvac_mode == "heat" else asleep.ceiling
        adaptive = bedtime_setback_heat if c.hvac_mode == "heat" else bedtime_setback_cool
        bedtime_target = adaptive if adaptive is not None else normal_bedtime

    windows_advice = _gives_window_advice(c, occupancy_mode)
    if windows is not None and not windows.periods and c.day_type in (DAY_TYPE_WARM, DAY_TYPE_MILD):
        # Issue #1230 R3b: the operation timeline is the plan; when it holds no window period today (the direction
        # rule or bedtime removed it) the briefing does not talk about opening windows either.
        windows_advice = False
    near_miss = _is_mild_near_miss(c, config["comfort_heat"], unoccupied=unoccupied, advice=windows_advice)

    close: _CloseFact | None = None
    evening_open: datetime | None = None
    hot_morning = hot_evening = False
    hot_evening_open: time | None = None
    events = warm_events or mild_events
    # A breach dated another day says nothing about today, so the briefing says less (no "around 2:00 PM").
    _breach = events.get("ceiling_breach_time") if events else None
    ceiling_breach = _local(_breach, now) if _breach is not None else None
    if ceiling_breach is not None and ceiling_breach.date() != now.date():
        ceiling_breach = None
    if c.day_type in (DAY_TYPE_WARM, DAY_TYPE_MILD) and windows_advice:
        cutoff = events.get("nat_vent_cutoff") if events else None
        # Never let the forecast cutoff produce a close at or before the open time: fall back to the static hour.
        if cutoff is not None and c.window_open_time and cutoff.time() <= c.window_open_time:
            cutoff = None
        already = bool(events and events.get("nat_vent_cutoff_already_reached"))
        close = _close_fact(cutoff, already, c.window_close_time, now)
        _open = events.get("evening_open_time") if events else None
        if close is not None and close.dynamic and cutoff is not None and _open is not None:
            _, ok = resolve_window_pair(cutoff, None, _open, None)
            evening_open = _local(_open, now) if ok is not None else None
    elif c.day_type == DAY_TYPE_HOT and not unoccupied:
        hot_morning = _window_event_available(hot_events, "nat_vent_cutoff", c.window_opportunity_morning)
        hot_evening = _window_event_available(hot_events, "evening_open_time", c.window_opportunity_evening)
        cutoff = hot_events.get("nat_vent_cutoff") if hot_events else None
        already = bool(hot_events and hot_events.get("nat_vent_cutoff_already_reached"))
        evening_dt = hot_events.get("evening_open_time") if hot_events else None
        if hot_morning:
            close = _close_fact(cutoff, already, c.window_opportunity_morning_end, now)
        if hot_morning and hot_evening:
            # Issue #878: close/open resolved TOGETHER so an open before its own close is never shown.
            _dyn_close = cutoff if close is not None and close.dynamic else None
            _, hot_evening_open = resolve_window_pair(
                _dyn_close, c.window_opportunity_morning_end, evening_dt, c.window_opportunity_evening_start
            )
            hot_evening = hot_evening_open is not None
        elif hot_evening:
            hot_evening_open = resolve_with_fallback(evening_dt, c.window_opportunity_evening_start)

    open_at: datetime | None = None
    window_veto = windows.vetoed if windows is not None and not unoccupied else None
    if windows is not None and not unoccupied:
        # Issue #1230 R3b: the window times the briefing states are the timeline's, never its own re-derivation.
        periods = [(o, e) for o, e in windows.periods if e > now]
        if c.day_type in (DAY_TYPE_WARM, DAY_TYPE_MILD) and windows_advice:
            if periods:
                close = _CloseFact(at=_local(periods[0][1], now), dynamic=True, already=False)
                _o = periods[0][0]
                open_at = _local(_o, now) if _o is not None and _local(_o, now) > now else None
                later = [o for o, _e in periods[1:] if o is not None]
                evening_open = _local(later[0], now) if later else None
            else:
                close = None
                evening_open = None
        elif c.day_type == DAY_TYPE_HOT:
            morning = [p for p in periods if p[0] is None or p[0].hour < 12]
            evening = [p for p in periods if p[0] is not None and p[0].hour >= 15]
            hot_morning = bool(morning)
            hot_evening = bool(evening)
            close = _CloseFact(at=_local(morning[0][1], now), dynamic=True, already=False) if morning else None
            hot_evening_open = _local(evening[0][0], now).time() if evening and evening[0][0] is not None else None
            hot_evening = hot_evening_open is not None

    # Overnight pre-cool: only when it is actually expected to run tonight and someone is home to be promised it.
    pre_cool_target = None if unoccupied else bedtime_setback_cool
    hot_pre_cool_target: float | None = None
    if c.day_type == DAY_TYPE_HOT and not unoccupied and runtime_config is not None:
        _modifier = resolve_pre_cool_modifier(c, runtime_config)
        if _modifier is not None:
            hot_pre_cool_target = compute_pre_cool_target(runtime_config, _modifier)

    return _BriefingFacts(
        occupancy_mode=occupancy_mode,
        unoccupied=unoccupied,
        band=band,
        awake_heat=awake_heat,
        awake_cool=awake_cool,
        normal_bedtime=normal_bedtime,
        bedtime_target=bedtime_target,
        windows_advice=windows_advice,
        near_miss=near_miss,
        close=close,
        evening_open=evening_open,
        ceiling_breach=ceiling_breach,
        hot_morning=hot_morning,
        hot_evening=hot_evening,
        hot_evening_open=hot_evening_open,
        pre_cool_target=pre_cool_target,
        hot_pre_cool_target=hot_pre_cool_target,
        economizer_threshold=config["comfort_cool"] + ECONOMIZER_TEMP_DELTA,
        vacation_deeper=vacation_deeper,
        open_at=open_at,
        window_veto=window_veto,
    )


def generate_briefing(
    classification: DayClassification,
    comfort_heat: float,
    comfort_cool: float,
    setback_heat: float,
    setback_cool: float,
    wake_time: time,
    sleep_time: time,
    learning_suggestions: list[str] | None = None,
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    manual_grace_seconds: int = DEFAULT_MANUAL_GRACE_SECONDS,
    automation_grace_seconds: int = DEFAULT_AUTOMATION_GRACE_SECONDS,
    grace_active: bool = False,
    grace_source: str | None = None,
    verbosity: str = "normal",
    fan_mode: str = FAN_MODE_DISABLED,
    occupancy_mode: str = "home",
    temp_unit: str = FAHRENHEIT,
    bedtime_setback_heat: float | None = None,
    bedtime_setback_cool: float | None = None,
    adaptive_thermal_active: bool = False,
    predicted_indoor_future: list[dict] | None = None,
    predicted_outdoor_future: list[dict] | None = None,
    runtime_config: dict | None = None,
    nat_vent_plan: dict | None = None,
    current_indoor_temp: float | None = None,
    current_outdoor_temp: float | None = None,
    overnight_heat_engaged: bool = False,
    automation_overridden: bool = False,
    now: datetime | None = None,
    timeline: Any | None = None,
    current_hold: Any | None = None,
    language: str = "en",
) -> str:
    """Generate the daily climate briefing message.

    ``current_hold`` (Issue #1404): the coordinator's ``current_hold()``. While a time-of-use bank owns the
    thermostat the HVAC Mode row quotes the bank target and when the schedule band returns, and Today's Steps opens
    with the same sentence Status shows; otherwise (None, or no bank) the briefing is unchanged.

    ``timeline`` (Issue #1230 R3b): the zone's operation timeline. When given, the briefing's window times are the
    timeline's window steps and a "Today's steps" section lists the timeline's step sentences; without it (direct
    calls, tests, a degraded coordinator) the briefing computes its facts as before.

    Args:
        classification: Today's day classification and recommendations.
        comfort_heat / comfort_cool: User's comfort setpoints.
        setback_heat / setback_cool: User's setback setpoints.
        wake_time / sleep_time: User's schedule.
        learning_suggestions: Any pending suggestions from the learning system.
        debounce_seconds: How long a door/window must be open before HVAC pauses.
        manual_grace_seconds: Grace period after a manual HVAC override.
        automation_grace_seconds: Grace period after Climate Advisor resumes HVAC.
        grace_active: Whether a grace period is currently active.
        grace_source: "manual" or "automation" if a grace period is active.
        verbosity: "tldr_only" (header + table only), "normal" (header + table + trimmed
            body), or "verbose" (header + table + full original body).
        fan_mode: Fan control mode — one of the FAN_MODE_* constants.
        occupancy_mode: Current occupancy state — "home", "away", "guest", or "vacation".
        temp_unit: Display unit — "fahrenheit" or "celsius".
        runtime_config: Full runtime config dict, used only to compute whether tonight's overnight
            pre-cool (``resolve_pre_cool_modifier()``) is expected to run, for the hot-day plan's
            narrative (Issue #558). If omitted, that narrative line is left out rather than
            guessed. Named distinctly from the local ``config`` dict built below (TLDR-table
            shape) to avoid shadowing it.
        nat_vent_plan: Issue #817 — the coordinator's single per-cycle
            ``nat_vent_plan.compute_nat_vent_plan()`` result
            (``self._nat_vent_plan``), when the caller already has one. When given, this
            is used directly instead of deriving a fresh one from
            ``predicted_indoor_future``/``predicted_outdoor_future`` — the same result the
            "Next Automation"/"Next User Action" status cards read this cycle, so the
            briefing can never disagree with them. When omitted (e.g. direct/standalone
            calls, tests), behavior is unchanged: derived locally from the prediction
            curves below.
        current_indoor_temp / current_outdoor_temp: Issue #847/#430 — live readings used
            only as a sanity check before the WARM/MILD-day plan text asserts a
            ``comfort_floor`` reason for the nat-vent close time: if outdoor has already
            risen above indoor by render time (the ``comfort_floor`` prediction's
            underlying condition no longer holds against live data), the text falls back
            to ``outdoor_rise`` phrasing instead of asserting a stale claim. Omitted
            (None), the sanity check cannot rule anything out and the reason is trusted
            as computed (unchanged prior behavior).
        overnight_heat_engaged: Issue #948 (Root Cause 1) — real runtime signal
            (``coordinator.get_hvac_runtime_today() > 0``) gating the MILD-day "I
            warmed to X before sunrise" claim and the WARM-day "HVAC is off this
            morning." claim, so neither is asserted unless heat actually ran
            overnight. Threaded identically into both ``_mild_day_plan()`` and
            ``_warm_day_plan()``. Default False (assume no evidence of overnight
            heat — the "don't assert what you can't verify" thesis this whole fix
            is built on) for callers that don't pass it; at this default,
            ``_mild_day_plan()`` renders the neutral wording (not "I warmed to X").
            Issue #1211: the WARM-day "HVAC is off this morning." sentence no longer
            exists (a comfort band is armed on a warm day); with this flag False and no
            window advice, ``_warm_day_plan()`` says the thermostat holds the band (and,
            only on a real forecast low, that outside air won't do much).
        automation_overridden: Issue #948 (Root Cause 1, third site) — whether
            automation is disabled, manually overridden, or paused (the same
            signal ``_compute_automation_status()`` reads). Gates
            ``_leaving_home_section()``'s away+cool/away+heat "I've applied
            setback..."/"I've dropped to X..." claims so they aren't asserted
            when the setback was never actually commanded. Default False for
            callers that don't pass it.

    Returns:
        Formatted briefing string suitable for email or notification.
    """
    c = classification
    lines: list[str] = []

    _LOGGER.debug(
        "Generating briefing — day_type=%s, trend=%s, comfort_heat=%.0f\u00b0F, comfort_cool=%.0f\u00b0F, verbosity=%s",
        c.day_type,
        c.trend_direction,
        comfort_heat,
        comfort_cool,
        verbosity,
    )

    # TLDR table — used standalone for push notifications, embedded in full briefing
    # Issue #1091: Prefer savings widens the awake home band the thermostat is armed with;
    # derive the flag once and the awake edges once so every display site quotes the same band.
    _savings = bool((runtime_config or {}).get("aggressive_savings", False))
    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": setback_heat,
        "setback_cool": setback_cool,
        "sleep_time": sleep_time,
        "wake_time": wake_time,
        "aggressive_savings": _savings,
    }
    awake_heat, awake_cool = awake_comfort_edges(config, aggressive_savings=_savings)
    # Issue #535: comfort-floor awareness for the nat-vent cutoff prediction — the real
    # activation gate (decide_nat_vent_gate()) requires indoor > comfort_heat as one of
    # its four conditions, but this predictive curve scan historically only modeled the
    # outdoor-vs-indoor half. When runtime_config is available, resolve comfort_heat_raw/
    # sleep_heat from it and pass an in_sleep_window_fn so compute_nat_vent_plan() can
    # also scan for a floor crossing. Omitted (None) when runtime_config isn't provided —
    # callers relying on that (e.g. tests that call generate_briefing() directly without
    # it) keep today's outdoor-only behavior rather than guessing at missing config.
    _comfort_heat_raw = float(runtime_config.get("comfort_heat", comfort_heat)) if runtime_config else None
    _sleep_heat = float(runtime_config.get("sleep_heat", comfort_heat)) if runtime_config else None
    _in_sleep_window_fn: Callable[[datetime], bool] | None = (
        (lambda ts: _in_sleep_window(ts, runtime_config)) if runtime_config else None
    )

    # Single source of truth for warm/mild-day window/AC timing (Issue #518, extended
    # in #817): when the caller already computed this cycle's nat_vent_plan (the same
    # value the status cards read), use it directly rather than deriving a second copy
    # here — this is what makes the header table and conversational body, plus every
    # other consumer, incapable of disagreeing about when windows close or whether AC
    # is expected. Falls back to a local derivation only when no plan was supplied
    # (direct/standalone calls, tests that pass raw prediction curves instead).
    if nat_vent_plan is not None:
        warm_events = nat_vent_plan if c.day_type == DAY_TYPE_WARM else None
        mild_events = nat_vent_plan if c.day_type == DAY_TYPE_MILD else None
        hot_events = nat_vent_plan if c.day_type == DAY_TYPE_HOT else None
    else:
        warm_events = (
            compute_nat_vent_plan(
                predicted_indoor=predicted_indoor_future,
                predicted_outdoor=predicted_outdoor_future,
                comfort_cool=comfort_cool,
                ceiling=awake_cool,
                comfort_heat_raw=_comfort_heat_raw,
                sleep_heat=_sleep_heat,
                in_sleep_window_fn=_in_sleep_window_fn,
                window_open_time=c.window_open_time,
            )
            if c.day_type == DAY_TYPE_WARM and predicted_indoor_future and predicted_outdoor_future
            else None
        )
        # Issue #534: MILD-day window close time was documented
        # (docs/08-COMPUTATION-REFERENCE.md §6d) as ODE-dynamic but never actually wired
        # up — _mild_day_plan() always used the static classifier hour. Uses
        # compute_nat_vent_plan() here too (the dead `_derive_natural_vent_events()`
        # sibling — built for a list[float] hour-indexed curve shape that
        # _build_predicted_indoor_future() has never actually produced — was removed in
        # #535).
        mild_events = (
            compute_nat_vent_plan(
                predicted_indoor=predicted_indoor_future,
                predicted_outdoor=predicted_outdoor_future,
                comfort_cool=comfort_cool,
                ceiling=awake_cool,
                comfort_heat_raw=_comfort_heat_raw,
                sleep_heat=_sleep_heat,
                in_sleep_window_fn=_in_sleep_window_fn,
                window_open_time=c.window_open_time,
            )
            if c.day_type == DAY_TYPE_MILD and predicted_indoor_future and predicted_outdoor_future
            else None
        )
        # Issue #876: HOT day's morning-opportunity close/evening-reopen times had no
        # dynamic path at all (unlike WARM/MILD above) — always the raw
        # window_opportunity_morning_end/evening_start hour constants, with no
        # connection to the actual forecast. Bounded by window_opportunity_morning_start
        # (HOT's own open-time field, distinct from WARM/MILD's window_open_time —
        # see nat_vent_plan.py's compute_nat_vent_plan() window_open_time param, which
        # is a generic "don't scan before this hour" bound regardless of which field
        # supplies it). comfort_heat_raw/sleep_heat/in_sleep_window_fn are intentionally
        # omitted — HOT's hvac_mode is "cool", so the comfort-floor scan (a heating-mode
        # concept) doesn't apply.
        hot_events = (
            compute_nat_vent_plan(
                predicted_indoor=predicted_indoor_future,
                predicted_outdoor=predicted_outdoor_future,
                comfort_cool=comfort_cool,
                ceiling=awake_cool,
                window_open_time=c.window_opportunity_morning_start,
            )
            if c.day_type == DAY_TYPE_HOT and predicted_indoor_future and predicted_outdoor_future
            else None
        )

    # Issue #1231: every fact the briefing states, gathered once here (the TLDR header's comfort band included).
    facts = _gather_briefing_facts(
        c,
        config,
        occupancy_mode=occupancy_mode,
        bedtime_setback_heat=bedtime_setback_heat,
        bedtime_setback_cool=bedtime_setback_cool,
        warm_events=warm_events,
        mild_events=mild_events,
        hot_events=hot_events,
        runtime_config=runtime_config,
        now=now,
        awake_edges=(awake_heat, awake_cool),
        windows=timeline_windows(timeline, (now or dt_util.now()).date()) if timeline is not None else None,
    )
    if current_hold is not None and hasattr(current_hold, "band"):
        facts = replace(facts, current_hold=current_hold)
    tldr_lines = _generate_tldr_table(c, config, temp_unit=temp_unit, facts=facts, language=language)

    if verbosity == "tldr_only":
        briefing_text = "\n".join(tldr_lines).rstrip()
        _LOGGER.debug(
            "Briefing generated (tldr_only) — %d chars",
            len(briefing_text),
        )
        return briefing_text

    # Structured header (kept for full briefing / email)
    # Note: Today/Tomorrow temps and Day Type are already in the TLDR table,
    # so we only include the title and separator to avoid duplication (Issue #52).
    lines.append(_t(language, "briefing.header_title"))
    lines.append(f"{'=' * 40}")
    lines.append("")
    lines.extend(tldr_lines)
    lines.append("")

    # Conversational body
    if c.day_type == DAY_TYPE_HOT:
        lines.extend(_hot_day_plan(c, facts, fan_mode=fan_mode, temp_unit=temp_unit, language=language))
    elif c.day_type == DAY_TYPE_WARM:
        lines.extend(
            _warm_day_plan(
                c,
                facts,
                fan_mode=fan_mode,
                temp_unit=temp_unit,
                warm_events=warm_events,
                current_indoor_temp=current_indoor_temp,
                current_outdoor_temp=current_outdoor_temp,
                overnight_heat_engaged=overnight_heat_engaged,
                language=language,
            )
        )
    elif c.day_type == DAY_TYPE_MILD:
        lines.extend(
            _mild_day_plan(
                c,
                facts,
                temp_unit=temp_unit,
                mild_events=mild_events,
                current_indoor_temp=current_indoor_temp,
                current_outdoor_temp=current_outdoor_temp,
                overnight_heat_engaged=overnight_heat_engaged,
                language=language,
            )
        )
    elif c.day_type == DAY_TYPE_COOL:
        lines.extend(_cool_day_plan(c, facts, temp_unit=temp_unit, language=language))
    elif c.day_type == DAY_TYPE_COLD:
        lines.extend(_cold_day_plan(c, facts, temp_unit=temp_unit, language=language))

    _LOGGER.debug("Dispatched %s day plan", c.day_type)

    lines.append("")
    lines.extend(
        _leaving_home_section(
            c,
            setback_heat,
            setback_cool,
            occupancy_mode=occupancy_mode,
            temp_unit=temp_unit,
            automation_overridden=automation_overridden,
            facts=facts,
            language=language,
        )
    )
    lines.append("")
    lines.extend(_fresh_air_section(c, facts, debounce_seconds, temp_unit=temp_unit, language=language))

    # Grace period status — only shown when a grace period is currently active,
    # or when grace periods are configured to a non-default value worth explaining
    grace_lines = _grace_period_section(
        debounce_seconds=debounce_seconds,
        manual_grace_seconds=manual_grace_seconds,
        automation_grace_seconds=automation_grace_seconds,
        grace_active=grace_active,
        grace_source=grace_source,
        language=language,
    )
    if grace_lines:
        lines.append("")
        lines.extend(grace_lines)
        _LOGGER.debug("Grace section included — source=%s", grace_source)

    _bank_line = hold_sentence(facts.current_hold, temp_unit) if facts.current_hold is not None else None
    if timeline is not None or _bank_line is not None:
        ops = operations_lines(timeline, now or dt_util.now()) if timeline is not None else []
        if _bank_line is not None:
            ops = [f"  • {_t(language, 'briefing.now_prefix')}{_bank_line}.", *ops]
        if ops:
            lines.append("")
            lines.append(_t(language, "briefing.todays_steps"))
            lines.append("-" * 40)
            lines.extend(ops)

    lines.append("")
    lines.extend(
        _tonight_preview(
            c,
            facts,
            temp_unit=temp_unit,
            adaptive_thermal_active=adaptive_thermal_active,
            language=language,
        )
    )

    # Learning suggestions (kept structured for accept/dismiss clarity)
    if learning_suggestions:
        lines.append("")
        lines.append(_t(language, "briefing.suggestions_header"))
        lines.append("-" * 40)
        for suggestion in learning_suggestions:
            lines.append(f"  \u2022 {suggestion}")
        lines.append("")
        lines.append(_t(language, "briefing.suggestions_footer"))

    briefing_text = "\n".join(lines)
    _LOGGER.debug(
        "Briefing generated — %d chars, %d learning suggestions",
        len(briefing_text),
        len(learning_suggestions) if learning_suggestions else 0,
    )
    if len(briefing_text) > 250:
        _LOGGER.debug("Briefing exceeds 250-char sensor state limit — full text available in sensor attribute")
    return briefing_text


def _window_event_available(events: dict | None, key: str, opportunity_flag: bool) -> bool:
    """Single shared check (Issue #878) for "is there a window event to show here" —
    used identically by both Hot render call sites (_generate_tldr_table() and
    _hot_day_plan()) so the eligibility-override rule can't be written twice and
    drift apart, which is exactly the duplication class this feature keeps failing on.

    A real ODE-computed crossing (``events[key] is not None``) always wins. The
    coarse today/tomorrow-low ``opportunity_flag`` heuristic only matters as a
    fallback signal when no forecast curve exists yet — it has no relationship to
    when an actual computed crossing falls, so it must never override real data.
    """
    return (events is not None and events.get(key) is not None) or opportunity_flag


def _says_air_wont_help(c: DayClassification) -> bool:
    """Whether the briefing may SAY outside air will not help today (Issue #1211).

    Only when the classifier decided so on a real forecast low: not recommended by the forecast answer nor the
    live flag, and the low is not a stand-in. When the low is estimated the answer is unknown, so the text
    asserts neither that air helps nor that it does not. Away/vacation never reaches this (no window advice there
    says nothing about the air at all).
    """
    return not c.forecast_windows_recommended and not c.windows_recommended and not c.low_is_estimated


def _band_text(floor: float, ceiling: float, temp_unit: str) -> str:
    """'68-74°F' -- the armed comfort band, quoted from the band the thermostat actually holds."""
    return f"{format_temp(floor, temp_unit)[:-2]}\u2013{format_temp(ceiling, temp_unit)}"


def _generate_tldr_table(
    c: DayClassification,
    config: dict,
    temp_unit: str = FAHRENHEIT,
    bedtime_setback_heat: float | None = None,
    bedtime_setback_cool: float | None = None,
    occupancy_mode: str = "home",
    warm_events: dict | None = None,
    mild_events: dict | None = None,
    hot_events: dict | None = None,
    facts: _BriefingFacts | None = None,
    now: datetime | None = None,
    language: str = "en",
) -> list[str]:
    """Generate a plain-text aligned TLDR summary table.

    Args:
        c: Today's day classification.
        config: Dict with comfort_heat, comfort_cool, setback_heat, setback_cool,
            sleep_time, wake_time keys.
        temp_unit: Display unit — "fahrenheit" or "celsius".
        facts: Issue #1231 — the briefing facts gathered once by ``generate_briefing()`` (every row reads
            from them, so the header can never disagree with the body). When omitted (direct calls, tests)
            they are gathered here from the remaining arguments, through the same function.
        bedtime_setback_heat / bedtime_setback_cool, occupancy_mode, warm_events / mild_events / hot_events,
        now: inputs to that gather; ignored when ``facts`` is given. The events are the result of
            compute_nat_vent_plan(), pre-computed once in generate_briefing() (Issue #518) so the header's
            window-close time always agrees with the conversational body.

    Returns:
        List of lines forming a plain-text aligned table.
    """
    if facts is None:
        facts = _gather_briefing_facts(
            c,
            config,
            occupancy_mode=occupancy_mode,
            bedtime_setback_heat=bedtime_setback_heat,
            bedtime_setback_cool=bedtime_setback_cool,
            warm_events=warm_events,
            mild_events=mild_events,
            hot_events=hot_events,
            now=now,
        )
    sleep_time = config["sleep_time"]
    occupancy_mode = facts.occupancy_mode

    # --- Day Type row ---
    day_type_val = f"{c.day_type.title()} ({format_temp(c.today_high, temp_unit)})"

    # --- HVAC Mode row (Issue #85: show setback temps when away/vacation) ---
    # Issue #1088: print the edge the thermostat is actually armed with -- vacation's extra setback
    # included -- by reading the engine's band selector (resolved once into the facts), not the raw config keys.
    # A morning snapshot: the TOU away/vacation widening is not threaded in. Issue #1091: the band reflects the
    # real Prefer savings flag, so the row quotes the awake band the thermostat is armed with.
    _band = facts.band
    _hold = facts.current_hold
    _bank = getattr(_hold, "bank", None)
    # Issue #1404 (mirrors): with the live hold, an empty house quotes the band in force (the Away/Vacation band
    # widened for a high-cost period: the thermostat is told cool 83, not 80), and an occupied, awake home quotes the
    # family the thermostat is armed in (after a cool bank: "Heat at 68", not "Cool at 76").
    _day_mode = c.hvac_mode
    if _hold is not None and _bank is None:
        if facts.unoccupied:
            _band = _hold.band
        elif c.hvac_mode in ("heat", "cool") and getattr(_hold, "family", None) in ("heat", "cool"):
            _day_mode = _hold.family
    if _bank is not None:
        # Issue #1404: a time-of-use bank owns the thermostat: the row quotes what it is told and what follows.
        _verb = _t(language, "briefing.verb_cool") if _bank.mode == "cool" else _t(language, "briefing.verb_heat")
        _until = dt_util.as_local(_bank.ends).strftime(_FMT_HOUR)
        hvac_val = _t(
            language,
            "briefing.hvac_bank",
            verb=_verb,
            target=format_temp(_bank.target, temp_unit),
            until=_until,
            after=hold_after_text(facts.current_hold, temp_unit),
        )
    elif facts.unoccupied:
        if _day_mode == "cool":
            hvac_val = _t(language, "briefing.cool_at", t=format_temp(_band.ceiling, temp_unit))
        elif _day_mode == "heat":
            hvac_val = _t(language, "briefing.heat_at", t=format_temp(_band.floor, temp_unit))
        else:
            # Issue #1253 (mirror of #1211): hvac_mode "off" on a warm/mild day is NOT "nothing runs" -- the
            # away/vacation band is armed (heat below its floor, cool above its ceiling). Name the held band.
            _label = (
                _t(language, "briefing.label_vacation")
                if occupancy_mode == "vacation"
                else _t(language, "briefing.label_away")
            )
            hvac_val = _t(
                language, "briefing.named_band", label=_label, band=_band_text(_band.floor, _band.ceiling, temp_unit)
            )
    elif _day_mode == "cool":
        hvac_val = _t(language, "briefing.cool_at", t=format_temp(_band.ceiling, temp_unit))
    elif _day_mode == "heat":
        hvac_val = _t(language, "briefing.heat_at", t=format_temp(_band.floor, temp_unit))
    else:
        # Issue #1211: hvac_mode "off" on a warm/mild day is NOT "off": the comfort band is armed (heat below
        # the floor, cool above the ceiling). Say what is held, and "(windows day)" only when it is true.
        hvac_val = _t(language, "briefing.comfort_band", band=_band_text(_band.floor, _band.ceiling, temp_unit))
        if facts.windows_advice:
            hvac_val += " " + _t(language, "briefing.windows_day_suffix")

    # --- Windows row ---
    # Issue #1231: every close time is the dated one from the facts (``effective_window_close()``), so a cutoff
    # dated another day is never shown as today's time; an empty house gets no window advice at all.
    threshold = facts.economizer_threshold
    if c.day_type != DAY_TYPE_HOT and facts.windows_advice and c.window_open_time and facts.close is not None:
        # Issue #876: the morning OPEN time is intentionally dropped from this row -- it carries no useful
        # information -- so the row shows only the close time, matching the conversational body.
        if facts.open_at is not None:
            windows_val = _t(
                language,
                "briefing.open_close_by",
                open=facts.open_at.strftime(_FMT_HOUR),
                close=facts.close.text,
            )
        else:
            windows_val = _t(language, "briefing.close_by", close=facts.close.text)
    elif facts.hot_morning and facts.hot_evening and facts.close is not None:
        # Issue #878: close/open resolved TOGETHER (see resolve_window_pair()) -- the incident (Close by 6:00 PM /
        # Open 5:00 PM+) is prevented when the facts were gathered.
        e_start = facts.hot_evening_open.strftime(_FMT_HOUR)
        windows_val = _t(
            language,
            "briefing.close_by_threshold_and_open",
            close=facts.close.text,
            threshold=format_temp(threshold, temp_unit),
            open=e_start,
        )
    elif facts.hot_morning and facts.close is not None:
        windows_val = _t(
            language, "briefing.close_by_threshold", close=facts.close.text, threshold=format_temp(threshold, temp_unit)
        )
    elif facts.hot_evening and facts.hot_evening_open is not None:
        windows_val = _t(language, "briefing.open_onward", open=facts.hot_evening_open.strftime(_FMT_HOUR))
    elif facts.window_veto == "heat":
        windows_val = _t(language, "briefing.closed_veto_heat")
    elif facts.window_veto == "warm":
        windows_val = _t(language, "briefing.closed_veto_warm")
    else:
        windows_val = _t(language, "briefing.closed_all_day")

    # --- Bedtime Setback row ---
    # Issue #1231: one bedtime number (adaptive when learned, else the normal bedtime setpoint) shared with the
    # body; while away/vacation the engine holds the away setback all night, so that is what the row says.
    sleep_str = sleep_time.strftime(_FMT_HOUR)
    if facts.unoccupied:
        bedtime_val = _t(language, "briefing.away_setback_held")
    elif facts.bedtime_target is not None:
        bedtime_val = _t(
            language, "briefing.bedtime_at", t=format_temp(facts.bedtime_target, temp_unit), when=sleep_str
        )
    else:
        bedtime_val = _t(language, "briefing.no_setback")

    # --- Tomorrow row ---
    trend_desc = _trend_description(c, temp_unit=temp_unit, language=language)
    tomorrow_val = f"{trend_desc} ({format_temp(c.tomorrow_high, temp_unit)})"

    rows = [
        f"  {_t(language, 'briefing.row_day_type')}: {day_type_val}",
        f"  {_t(language, 'briefing.row_hvac_mode')}: {hvac_val}",
    ]
    # Issue #85: show occupancy status when not home
    if occupancy_mode == "away":
        rows.append(f"  {_t(language, 'briefing.occ_away')}")
    elif occupancy_mode == "vacation":
        # Issue #1369: "deep" only when the vacation band is deeper than away on the edge that matters.
        if facts.vacation_deeper:
            rows.append(f"  {_t(language, 'briefing.occ_vacation_deep')}")
        else:
            rows.append(f"  {_t(language, 'briefing.occ_vacation')}")
    elif occupancy_mode == "guest":
        rows.append(f"  {_t(language, 'briefing.occ_guest')}")
    rows.extend(
        [
            f"  {_t(language, 'briefing.row_windows')}: {windows_val}",
            f"  {_t(language, 'briefing.row_bedtime_setback')}: {bedtime_val}",
            f"  {_t(language, 'briefing.row_tomorrow')}: {tomorrow_val}",
        ]
    )
    return rows


def _trend_description(c: DayClassification, temp_unit: str = FAHRENHEIT, language: str = "en") -> str:
    """Human-readable trend description."""
    if c.trend_direction == "warming":
        if c.trend_magnitude >= 10:
            return _t(
                language, "briefing.trend_sig_warmer", delta=format_temp_delta(c.trend_magnitude, temp_unit)
            )
        return _t(language, "briefing.trend_warming", delta=format_temp_delta(c.trend_magnitude, temp_unit))
    elif c.trend_direction == "cooling":
        if c.trend_magnitude >= 10:
            return _t(
                language, "briefing.trend_sig_cooler", delta=format_temp_delta(c.trend_magnitude, temp_unit)
            )
        return _t(language, "briefing.trend_cooling", delta=format_temp_delta(c.trend_magnitude, temp_unit))
    return _t(language, "briefing.trend_stable")


def _hot_day_plan(
    c,
    facts: _BriefingFacts,
    fan_mode: str = FAN_MODE_DISABLED,
    temp_unit: str = FAHRENHEIT,
    language: str = "en",
) -> list[str]:
    """Conversational plan for hot days (85\u00b0F+).

    Issue #1231: every value comes from ``facts``. An empty house (away/vacation) gets the lead line only --
    the engine holds the away setback all day and night, so no hold temperature, pre-cool, window or blind
    advice is promised.

    Issue #876: morning-close and evening-open times are derived from the same ODE-crossing mechanism
    WARM/MILD use (nat_vent_plan.compute_nat_vent_plan()), falling back to the static classifier hour when no
    forecast curve is available yet (resolved in ``_gather_briefing_facts()`` through
    ``effective_window_close()``); the morning OPEN time is intentionally dropped from the displayed text.

    Issue #878: has_morning/has_evening reflect whether a REAL crossing was computed, not just the coarse
    today/tomorrow-low eligibility heuristic, and close/open are resolved TOGETHER via resolve_window_pair() so
    a pairing like "Close by 6:00 PM / Open 5:00 PM+" can never render.
    """
    if facts.unoccupied:
        return [_t(language, "briefing.hot_unoccupied")]

    threshold = facts.economizer_threshold
    # Issue #1091: display-only armed ceiling (Prefer savings widens it); thresholds stay on raw comfort_cool.
    _hold = facts.awake_cool
    close = facts.close
    has_morning = facts.hot_morning and close is not None
    has_evening = facts.hot_evening and facts.hot_evening_open is not None

    # Issue #558: only claim overnight pre-cool banking when it's actually expected to run tonight
    # (resolve_pre_cool_modifier() -- the same gate handle_pre_cool() uses), and phrase it prospectively.
    if facts.hot_pre_cool_target is not None:
        opener = _t(language, "briefing.hot_precool_opener", t=format_temp(facts.hot_pre_cool_target, temp_unit))
    else:
        opener = _t(
            language,
            "briefing.hot_hold_opener",
            high=format_temp(c.today_high, temp_unit),
            hold=format_temp(_hold, temp_unit),
        )
    lines = [opener]

    if has_morning:
        # Issue #878-followup: "already reached" is only meaningful when the dynamic cutoff is in play -- a
        # static fallback hour (including a cutoff dated another day) is a plain configured hour.
        lines.append("")
        if close.already:
            lines.append(_t(language, "briefing.hot_already_above", t=format_temp(threshold, temp_unit)))
        else:
            lines.append(
                _t(
                    language,
                    "briefing.hot_close_around",
                    t=format_temp(threshold, temp_unit),
                    close=close.text,
                )
            )
        lines.append("")
        lines.append(
            _t(
                language,
                "briefing.hot_once_closed",
                hold=format_temp(_hold, temp_unit),
                tail=_t(language, "briefing.tail_period" if has_evening else "briefing.tail_rest_of_day"),
            )
        )
        if has_evening:
            lines.append("")
            lines.append(
                describe_reopen_clause(facts.hot_evening_open.strftime(_FMT_HOUR), reopening_from_closed_all_day=False)
            )
    elif has_evening:
        lines.append("")
        lines.append(_t(language, "briefing.hot_sealed_blinds_hold", hold=format_temp(_hold, temp_unit)))
        lines.append("")
        lines.append(
            describe_reopen_clause(facts.hot_evening_open.strftime(_FMT_HOUR), reopening_from_closed_all_day=True)
        )
    else:
        lines.append("")
        lines.append(_t(language, "briefing.hot_sealed_blinds_handle", hold=format_temp(_hold, temp_unit)))

    if fan_mode != FAN_MODE_DISABLED and (has_morning or has_evening):
        lines.append("")
        lines.append(_t(language, "briefing.hot_fan_helps"))
    return lines


def _warm_day_plan(
    c,
    facts: _BriefingFacts,
    fan_mode: str = FAN_MODE_DISABLED,
    temp_unit: str = FAHRENHEIT,
    warm_events: dict | None = None,
    current_indoor_temp: float | None = None,
    current_outdoor_temp: float | None = None,
    overnight_heat_engaged: bool = False,
    language: str = "en",
) -> list[str]:
    """Conversational plan for warm days (75-85\u00b0F).

    Issue #518: window/AC timing is derived once in generate_briefing() and passed
    in as `warm_events` so this never disagrees with the header table \u2014 this is now
    the only source (Issue #817 removed the local-recompute fallback, which no
    caller, production or test, ever actually reached).

    Issue #847: the reason \u2192 phrase mapping now flows through the shared
    ``describe_nat_vent_cutoff_reason()`` helper (``nat_vent_plan.py``) instead of an
    inline branch, so this can never phrase ``nat_vent_cutoff_reason`` differently
    than the "Next Automation" status card (``coordinator.py``'s
    ``_compute_next_automation_action()``), which calls the same helper. Also closes
    #430 (filed alongside #428/#535 and never fixed): before asserting a
    ``comfort_floor`` reason in text, a live outdoor-vs-indoor sanity check
    (``free_cooling_direction_ok()``, the same #428 guard) confirms the reason still
    holds \u2014 if outdoor has already risen above indoor by render time, the sentence
    falls back to ``outdoor_rise`` phrasing instead of asserting a stale claim.

    Issue #948 (Root Cause 1; the sentence itself was removed by Issue #1211, which keeps this gate on the
    "heater ran overnight" alternative): the "HVAC is off this morning." sentence used to
    be asserted purely from classifier/window state (windows_recommended
    False), with no check on whether HVAC actually ran overnight — the same
    false present-tense claim shape as ``_mild_day_plan()``'s "I warmed"
    opening. ``overnight_heat_engaged`` (same real-runtime signal) gates it:
    when heat DID engage overnight, a brief factual alternative replaces the
    claim instead. Default False preserves prior behavior for callers that
    don't pass the signal.

    Issue #1211: every window sentence (the tip, the fan-ventilation line, "close up by", "reopen") is gated
    on ``windows_advice`` -- windows recommended today AND someone home (default: ``c.windows_recommended``).
    Otherwise the plan says what is true: the thermostat holds the comfort band. ``away`` (away/vacation) gets a
    neutral line with no claim about the air and no awake band; "outside air won't do much" is said only when
    the classifier decided so on a real forecast low (``_says_air_wont_help()``), and an estimated low asserts
    neither way.
    """
    lines = []
    windows_advice = facts.windows_advice
    away = facts.unoccupied
    # Issue #1211: the band the thermostat actually holds today, quoted from the armed edges.
    _held_cool = facts.awake_cool
    _band_desc = _band_text(facts.awake_heat, _held_cool, temp_unit)
    # Issue #1093: the AC-handoff sentences quote the ceiling the thermostat is armed with (Prefer
    # savings widens it), read from awake_comfort_edges() -- never comfort_cool + margin here.
    _hold = _held_cool

    _events = warm_events
    # Issue #1231: the close is the dated one from the facts. A forecast cutoff dated another day is the static
    # path (no dynamic reason / already-reached phrasing); a cutoff in play carries its reason.
    _close = facts.close
    _dynamic = _close is not None and _close.dynamic
    _nat_vent_cutoff_reason = _events.get("nat_vent_cutoff_reason") if _events and _dynamic else None
    _ceiling_breach = facts.ceiling_breach  # dated today only (Issue #1231)
    _evening_open_time = facts.evening_open  # Issue #940: pair-validated against the close when gathered

    # Issue #876: the morning OPEN time is intentionally dropped from this sentence \u2014
    # it carries no useful information (windows day starts, full stop). Only the
    # dynamically-computed close time matters here.
    if windows_advice and c.window_open_time:
        if _dynamic:
            close_t = _close.at.strftime(_FMT_HOUR)
            # Issue #878-followup: the ODE crossing scan is forward-only from "now", so
            # when the close condition is already true at render time, the scan can only
            # return the nearest future grid point \u2014 not a real future prediction. Asserting
            # it as "Close up at {HH:MM}" hours after the real (earlier) crossing already
            # passed is a false future-scheduled claim.
            _already = _close.already
            # Issue #535: two distinct reasons the cutoff can fire \u2014 outdoor air rising
            # above indoor (the original predicate), or indoor forecast to reach the
            # comfort floor first. Same close time either way; different sentence why
            # (Issue #847: phrasing now shared via describe_nat_vent_cutoff_reason()).
            _effective_reason = _nat_vent_cutoff_reason
            if _effective_reason == "comfort_floor" and not free_cooling_direction_ok(
                current_outdoor_temp, current_indoor_temp
            ):
                # #430 live sanity check: comfort_floor was predicted, but outdoor has
                # already risen above indoor by render time \u2014 the comfort_floor risk
                # this text is about to assert is no longer live. Fall back to
                # outdoor_rise phrasing rather than stating a stale reason.
                _LOGGER.warning(
                    "Nat-vent cutoff reason overridden for briefing text \u2014 comfort_floor no longer"
                    " live at render time: indoor=%.1f\u00b0F outdoor=%.1f\u00b0F (outdoor has already risen"
                    " above indoor). reason %s -> outdoor_rise",
                    current_indoor_temp,
                    current_outdoor_temp,
                    _nat_vent_cutoff_reason,
                )
                _effective_reason = "outdoor_rise"
            if _already and _effective_reason == "outdoor_rise":
                # "before outdoor air warms past indoor" reads backwards once that's
                # already happened \u2014 a self-contained present-tense sentence instead
                # of composing the (now-wrong-tense) reason fragment.
                lines.append(_t(language, "briefing.warm_close_now"))
            else:
                close_phrase = describe_close_timing(close_t, _already)
                prep = "" if _already else _t(language, "briefing.at_prep")
                lines.append(
                    _t(
                        language,
                        "briefing.warm_close_at",
                        prep=prep,
                        close_phrase=close_phrase,
                        reason=describe_nat_vent_cutoff_reason(_effective_reason),
                    )
                )
        else:
            lines.append(_t(language, "briefing.warm_windows_open_morning"))
    elif away:
        # Nobody is home: no claim about the air, and no awake band (the away section quotes the setbacks).
        lines.append(_t(language, "briefing.warm_unoccupied"))
    elif overnight_heat_engaged:
        lines.append(_t(language, "briefing.warm_heater_ran_overnight", band=_band_desc))
    elif _says_air_wont_help(c):
        lines.append(_t(language, "briefing.warm_air_wont_help", band=_band_desc))
    else:
        # Low is estimated: say neither that windows help nor that they don't.
        lines.append(_t(language, "briefing.warm_holds_band", band=_band_desc))

    if windows_advice and fan_mode != FAN_MODE_DISABLED:
        lines.append(_t(language, "briefing.warm_fan_boost"))

    lines.append("")

    # Issue #518: this used to independently promise "I'll run the AC starting around
    # X \u2014 no action needed from you", ignoring window state entirely \u2014 contradicting
    # the real automation guard (automation.py apply_classification()'s DEFER_PAUSED
    # branch actually suppresses AC the whole time a window is open) and duplicating
    # _fresh_air_section()'s already-correct, debounce-aware version of this same fact.
    # This section now only states the forecast and ties the AC to windows being
    # closed; _fresh_air_section owns the debounce/pause mechanics, so it's said once.
    if away:
        # Nobody is home: the AC-handoff sentences quote the awake ceiling, which is not what the thermostat holds
        # while away (the away section states the setback band).
        lines.pop()  # drop the spacer so the away line is not followed by a double blank
    elif _ceiling_breach is not None:
        breach_t = _ceiling_breach.strftime(_FMT_HOUR)
        _tail = _t(language, "briefing.ac_auto_tail_windows" if windows_advice else "briefing.ac_auto_tail")
        lines.append(
            _t(
                language,
                "briefing.indoor_forecast_reach",
                hold=format_temp(_hold, temp_unit),
                t=breach_t,
                tail=_tail,
            )
        )
    elif windows_advice and _close is not None and not _close.already:
        lines.append(
            _t(
                language,
                "briefing.warm_close_before_climb",
                close=_close.text,
                hold=format_temp(_hold, temp_unit),
            )
        )
    elif windows_advice and _close is None:
        lines.append(_t(language, "briefing.warm_ac_safety_net", hold=format_temp(_hold, temp_unit)))
    else:
        lines.append(_t(language, "briefing.warm_ac_steps_in", hold=format_temp(_hold, temp_unit)))

    # Pre-cool night mention: warming trend + a lower sleep ceiling is planned
    if facts.pre_cool_target is not None and getattr(c, "setback_modifier", 0.0) < 0:
        lines.append("")
        lines.append(_t(language, "briefing.warm_precool_tonight", t=format_temp(facts.pre_cool_target, temp_unit)))

    if windows_advice and _evening_open_time is not None:
        rec_t = _evening_open_time.strftime(_FMT_HOUR)
        # Issue #876 (renamed from recovery_time/nat_vent_recovers): nat_vent_plan.py's
        # compute_nat_vent_plan() only ever populates evening_open_time for an
        # "outdoor_rise" cutoff \u2014 for a "comfort_floor" cutoff (an early/cold-morning
        # close held for comfort, not outdoor heat) reopening was never a genuine,
        # actionable later event, so evening_open_time is always None there and this
        # block is never reached for that reason. Only the outdoor_rise phrasing
        # applies here.
        recovery_reason = _t(language, "briefing.when_evening_cools")
        # Issue #518: only claim "I'll turn off the AC" when the AC could
        # plausibly have engaged first (breach predicted before recovery) \u2014
        # otherwise this contradicted itself by canceling an action that was
        # never actually started.
        if _ceiling_breach is not None and _ceiling_breach < _evening_open_time:
            lines.append(_t(language, "briefing.reopen_turn_off_ac", t=rec_t, reason=recovery_reason))
        else:
            lines.append(_t(language, "briefing.reopen_around", t=rec_t, reason=recovery_reason))

    return lines


def _mild_day_plan(
    c,
    facts: _BriefingFacts,
    temp_unit: str = FAHRENHEIT,
    mild_events: dict | None = None,
    current_indoor_temp: float | None = None,
    current_outdoor_temp: float | None = None,
    overnight_heat_engaged: bool = True,
    language: str = "en",
) -> list[str]:
    """Conversational plan for mild days (60-74\u00b0F).

    Issue #847: previously had no reason branch at all \u2014 always said "to trap the
    warmth" regardless of ``nat_vent_cutoff_reason``, an asymmetry with
    ``_warm_day_plan()`` (which branched, just via its own now-removed inline
    if/else) that let MILD and WARM day text drift apart from each other. Now shares
    the same ``describe_nat_vent_cutoff_reason()`` helper and the same #430 live
    sanity check as ``_warm_day_plan()``, so the two day types (and the "Next
    Automation" card) can't disagree about what a given reason means.

    Issue #948 (Root Cause 1): the opening line used to unconditionally claim
    "I warmed to X before sunrise" regardless of whether heat ever actually ran
    \u2014 a house that coasts at comfort_heat purely on thermal mass has zero HVAC
    runtime, making that claim false. ``overnight_heat_engaged`` (real runtime
    signal, ``coordinator.get_hvac_runtime_today() > 0``) gates which opening
    line renders. Default True preserves prior behavior for callers that don't
    pass the signal (e.g. direct/standalone calls, older tests).

    Issue #1211: the window tip, the "close up by" line and the evening reopen line are gated on
    ``windows_advice`` (windows recommended today AND someone home; default ``c.windows_recommended``), and the
    plan never claims "HVAC is off" -- the comfort band is armed. ``near_miss`` (forecast high just under the
    floor) says outside air may help later instead of going silent (owner decision O7). ``away`` gets a neutral
    line with no claim about the air and no awake band; "isn't going to warm the house" is said only when the
    classifier decided so on a real forecast low (``_says_air_wont_help()``), and an estimated low asserts
    neither way.
    """
    windows_advice = facts.windows_advice
    away = facts.unoccupied
    near_miss = facts.near_miss
    # Issue #1091: display-only armed floor (Prefer savings widens it).
    _shown_heat = facts.awake_heat
    _band_desc = _band_text(_shown_heat, facts.awake_cool, temp_unit)
    _holding = _t(language, "briefing.thermostat_holds_lc", band=_band_desc)
    if not windows_advice:
        if away:
            # Nobody is home: no claim about the air, and no awake band (the away section quotes the setbacks).
            lines = [_t(language, "briefing.mild_unoccupied")]
        elif _says_air_wont_help(c):
            lines = [_t(language, "briefing.mild_air_wont_help", holding=_holding)]
        else:
            # Low is estimated: the answer is unknown, so say neither that windows help nor that they don't.
            lines = [_t(language, "briefing.mild_default", holding=_holding)]
        if near_miss:
            lines.append("")
            lines.append(
                _t(
                    language,
                    "briefing.mild_near_miss",
                    high=format_temp(c.today_high, temp_unit),
                    floor=format_temp(_shown_heat, temp_unit),
                )
            )
    elif overnight_heat_engaged:
        lines = [
            _t(language, "briefing.mild_warmed_before_sunrise", t=format_temp(_shown_heat, temp_unit), holding=_holding)
        ]
    else:
        lines = [_t(language, "briefing.mild_held_on_own", t=format_temp(_shown_heat, temp_unit))]

    if windows_advice and c.window_open_time:
        # Issue #876: the morning OPEN time is intentionally dropped from this
        # sentence \u2014 it carries no useful information.
        lines.append("")
        lines.append(_t(language, "briefing.mild_open_south_east"))

    # Issue #534: prefer the ODE-derived cutoff when available, matching docs/08-COMPUTATION-REFERENCE.md
    # \u00a76d. Issue #1231: the close is the dated one from the facts (``effective_window_close()``) -- a cutoff
    # dated another day is the static path (no reason, no already-reached phrasing).
    _close = facts.close
    _dynamic = _close is not None and _close.dynamic
    _mild_cutoff_reason = mild_events.get("nat_vent_cutoff_reason") if mild_events and _dynamic else None
    if windows_advice and _close is not None:
        # Issue #878-followup: "already reached" is only meaningful when the dynamic cutoff is in play.
        _already = _close.already
        # Issue #847: same reason branch + #430 live sanity check as _warm_day_plan()
        # \u2014 only meaningful when the ODE-derived cutoff (with its reason) is actually
        # in play; the static classifier-hour fallback has no reason to branch on.
        _effective_reason = _mild_cutoff_reason if _dynamic else None
        if _effective_reason == "comfort_floor" and not free_cooling_direction_ok(
            current_outdoor_temp, current_indoor_temp
        ):
            _LOGGER.warning(
                "Nat-vent cutoff reason overridden for briefing text \u2014 comfort_floor no longer"
                " live at render time: indoor=%.1f\u00b0F outdoor=%.1f\u00b0F (outdoor has already risen"
                " above indoor). reason %s -> outdoor_rise",
                current_indoor_temp,
                current_outdoor_temp,
                _mild_cutoff_reason,
            )
            _effective_reason = "outdoor_rise"
        lines.append("")
        if _already and _effective_reason == "outdoor_rise":
            lines.append(
                _t(language, "briefing.mild_close_now", t=format_temp(_shown_heat, temp_unit))
            )
        else:
            reason_fragment = (
                _t(language, "briefing.trap_the_warmth")
                if not _dynamic
                else describe_nat_vent_cutoff_reason(_effective_reason)
            )
            prep = "" if _already else _t(language, "briefing.by_prep")
            lines.append(
                _t(
                    language,
                    "briefing.mild_close_by",
                    prep=prep,
                    close=_close.text,
                    reason=reason_fragment,
                    t=format_temp(_shown_heat, temp_unit),
                )
            )

    # Issue #876: MILD days had no evening-reopen sentence at all \u2014 mirroring _warm_day_plan()'s equivalent
    # (outdoor_rise-only; see nat_vent_plan.py's compute_nat_vent_plan() docstring). Issue #940: the open is
    # pair-validated against the dynamic close when the facts are gathered.
    if windows_advice and facts.evening_open is not None:
        rec_t = facts.evening_open.strftime(_FMT_HOUR)
        lines.append("")
        _reason = _t(language, "briefing.when_evening_cools")
        lines.append(_t(language, "briefing.reopen_around", t=rec_t, reason=_reason))

    return lines


def _cool_day_plan(c, facts: _BriefingFacts, temp_unit: str = FAHRENHEIT, language: str = "en") -> list[str]:
    """Conversational plan for cool days (45-59\u00b0F).

    Issue #1231: an empty house (away/vacation) gets the lead line only -- the engine holds the away setback
    all day and night, so no hold temperature and no bedtime set-back is promised. At home the bedtime number is
    the one the TLDR row shows (``facts.bedtime_target``).
    """
    if facts.unoccupied:
        return [_t(language, "briefing.cool_unoccupied")]
    # Issue #1091: display-only armed floor (Prefer savings widens it).
    lines = [_t(language, "briefing.cool_heater_day", t=format_temp(facts.awake_heat, temp_unit))]
    if facts.bedtime_target is not None:
        lines.append("")
        lines.append(_t(language, "briefing.cool_bedtime_setback", t=format_temp(facts.bedtime_target, temp_unit)))
    return lines


def _cold_day_plan(c, facts: _BriefingFacts, temp_unit: str = FAHRENHEIT, language: str = "en") -> list[str]:
    """Conversational plan for cold days (below 45\u00b0F).

    Issue #1231: an empty house (away/vacation) gets the lead line only. At home, the bedtime target is compared
    against the NORMAL bedtime setpoint (``facts.normal_bedtime``, the edge the engine arms at bedtime) -- never
    against ``setback_heat``, which is the AWAY setpoint. The engine arms that same edge every heating night, so
    when the two are the same number there is no cold-day adjustment to describe (owner decision, option A): the
    plain "At bedtime I'll set back to X." Only a learned value that differs is a "conservative setback ... instead
    of the usual" one. Body and TLDR quote the same number.
    """
    if facts.unoccupied:
        return [_t(language, "briefing.cold_unoccupied")]
    lines = [_t(language, "briefing.cold_day_help")]

    if c.pre_condition and c.trend_direction == "cooling":
        # Issue #1211: this used to promise "I'll bump to X around 7pm for a couple hours". Nothing performs
        # that (the pre-heat intent is written to config and never read), so say only what is true.
        lines.append("")
        lines.append(_t(language, "briefing.cold_tomorrow_colder"))

    if facts.bedtime_target is not None:
        _target = format_temp(facts.bedtime_target, temp_unit)
        lines.append("")
        if facts.normal_bedtime is None or format_temp(facts.normal_bedtime, temp_unit) == _target:
            lines.append(_t(language, "briefing.cold_bedtime_setback", t=_target))
        else:
            lines.append(
                _t(
                    language,
                    "briefing.cold_conservative_setback",
                    t=_target,
                    usual=format_temp(facts.normal_bedtime, temp_unit),
                )
            )

    return lines


def _leaving_home_section(
    c,
    setback_heat,
    setback_cool,
    occupancy_mode: str = "home",
    temp_unit: str = FAHRENHEIT,
    automation_overridden: bool = False,
    facts: _BriefingFacts | None = None,
    language: str = "en",
) -> list[str]:
    """Conversational section about what happens when they leave.

    Args:
        c: Today's day classification.
        setback_heat: Heating setback temperature.
        setback_cool: Cooling setback temperature.
        occupancy_mode: Current occupancy state — "home", "away", "guest", or "vacation".
        temp_unit: Display unit — "fahrenheit" or "celsius".
        automation_overridden: Issue #948 (Root Cause 1, third site) — whether
            automation is disabled, manually overridden, or paused (the same
            signal ``_compute_automation_status()`` already reads). The
            away+cool/away+heat branches below used to unconditionally claim
            "I've applied setback temperatures..."/"I've dropped to X..." even
            though a disabled/overridden/paused automation never actually
            commanded that setback. When True, a hedged phrasing is used
            instead that doesn't claim the setback was actually applied.
            Default False preserves prior behavior for callers that don't
            pass the signal.
        facts: Issue #1369 -- the briefing facts. On vacation, "deeper" is said only when
            ``facts.vacation_deeper``; otherwise the vacation band from ``facts.band`` is quoted
            neutrally (a separate vacation setpoint may be equal to or milder than away). None keeps
            the pre-#1347 wording (vacation was always away -/+ 3, i.e. deeper).
    """
    if occupancy_mode == "vacation":
        if facts is not None and not facts.vacation_deeper:
            return [
                _t(
                    language,
                    "briefing.vacation_band",
                    band=_band_text(facts.band.floor, facts.band.ceiling, temp_unit),
                )
            ]
        return [_t(language, "briefing.vacation_deeper")]
    elif occupancy_mode == "guest":
        return [_t(language, "briefing.guest_visiting")]
    elif occupancy_mode == "away":
        if c.hvac_mode == "cool":
            if automation_overridden:
                return [_t(language, "briefing.away_cool_overridden", t=format_temp(setback_cool, temp_unit))]
            return [_t(language, "briefing.away_cool_applied", t=format_temp(setback_cool, temp_unit))]
        elif c.hvac_mode == "heat":
            if automation_overridden:
                return [_t(language, "briefing.away_heat_overridden", t=format_temp(setback_heat, temp_unit))]
            return [_t(language, "briefing.away_heat_applied", t=format_temp(setback_heat, temp_unit))]
        else:
            # Issue #1211: the comfort band is armed on a warm/mild day (HVAC mode "off" is not "nothing runs"),
            # so say what is held while away instead of "the HVAC is off".
            return [_t(language, "briefing.away_band", band=_band_text(setback_heat, setback_cool, temp_unit))]
    else:
        # occupancy_mode == "home" — default hypothetical text
        if c.hvac_mode == "cool":
            return [
                _t(
                    language,
                    "briefing.home_leave_cool",
                    minutes=OCCUPANCY_SETBACK_MINUTES,
                    t=format_temp(setback_cool, temp_unit),
                )
            ]
        elif c.hvac_mode == "heat":
            return [
                _t(
                    language,
                    "briefing.home_leave_heat",
                    t=format_temp(setback_heat, temp_unit),
                    minutes=OCCUPANCY_SETBACK_MINUTES,
                )
            ]
        else:
            return [
                _t(
                    language,
                    "briefing.home_leave_band",
                    minutes=OCCUPANCY_SETBACK_MINUTES,
                    band=_band_text(setback_heat, setback_cool, temp_unit),
                )
            ]


def _fresh_air_section(
    c,
    facts: _BriefingFacts,
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    temp_unit: str = FAHRENHEIT,
    natural_vent_active: bool = False,
    current_outdoor_temp: float | None = None,
    language: str = "en",
) -> list[str]:
    """User-centric section about opening windows/doors for fresh air.

    Issue #1231: while the house is empty (away/vacation) the thermostat is holding the setback band, not the
    awake band, so no branch quotes the awake comfort value ("pull back down to 76\u00b0F", "hold your 76\u00b0F
    target", "holding 68-74\u00b0F today") -- it is left out rather than quoting the wrong value (Issue #1211).

    Affirms the user's choice first, then explains impact and recovery.
    Varies by HVAC mode since the impact differs significantly.
    Uses the configured debounce duration so the timing matches actual behavior.
    """
    debounce_minutes = max(1, debounce_seconds // 60)
    debounce_desc = _t(
        language, "briefing.minute" if debounce_minutes == 1 else "briefing.minutes", n=debounce_minutes
    )
    away = facts.unoccupied
    comfort_cool = facts.awake_cool

    if natural_vent_active and current_outdoor_temp is not None:
        _target = "" if away else _t(language, "briefing.fresh_hold_target", t=format_temp(comfort_cool, temp_unit))
        return [
            _t(
                language,
                "briefing.fresh_natvent_active",
                t=format_temp(current_outdoor_temp, temp_unit),
                target=_target,
            )
        ]

    if c.hvac_mode == "cool":
        _pull_back = (
            ""
            if away
            else _t(language, "briefing.fresh_pull_back", t=format_temp(comfort_cool, temp_unit))
        )
        return [
            _t(language, "briefing.fresh_cool", debounce=debounce_desc, pull_back=_pull_back)
        ]
    elif c.hvac_mode == "heat":
        return [_t(language, "briefing.fresh_heat", debounce=debounce_desc)]
    else:
        _band = _band_text(facts.awake_heat, comfort_cool, temp_unit)
        _holding_clause = "" if away else _t(language, "briefing.fresh_holding_clause", band=_band)
        return [_t(language, "briefing.fresh_off", holding=_holding_clause, debounce=debounce_desc)]


def _grace_period_section(
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    manual_grace_seconds: int = DEFAULT_MANUAL_GRACE_SECONDS,
    automation_grace_seconds: int = DEFAULT_AUTOMATION_GRACE_SECONDS,
    grace_active: bool = False,
    grace_source: str | None = None,
    language: str = "en",
) -> list[str]:
    """Explain active grace periods and configured timings.

    Only included in the briefing when a grace period is currently active,
    so users aren't surprised that door/window sensors aren't pausing HVAC.
    Returns an empty list when there is nothing noteworthy to report.
    """
    if not grace_active or not grace_source:
        return []

    if grace_source == "manual":
        grace_minutes = max(1, manual_grace_seconds // 60)
        grace_desc = _t(language, "briefing.minute" if grace_minutes == 1 else "briefing.minutes", n=grace_minutes)
        return [_t(language, "briefing.grace_manual", grace=grace_desc)]
    else:
        # automation grace
        grace_minutes = max(1, automation_grace_seconds // 60)
        grace_desc = _t(language, "briefing.minute" if grace_minutes == 1 else "briefing.minutes", n=grace_minutes)
        return [_t(language, "briefing.grace_automation", grace=grace_desc)]


def _tonight_preview(
    c,
    facts: _BriefingFacts,
    temp_unit: str = FAHRENHEIT,
    adaptive_thermal_active: bool = False,
    language: str = "en",
) -> list[str]:
    """Conversational preview of tonight and tomorrow based on trend.

    Issue #1231: while the house is empty the engine holds the away setback all night, so no "I'll set back more
    aggressively tonight" promise and no bedtime-tuning line is made.
    """
    _LOGGER.debug(
        "Tonight preview \u2014 trend=%s, magnitude=%.1f\u00b0F",
        c.trend_direction,
        c.trend_magnitude,
    )
    if c.trend_direction == "warming" and c.trend_magnitude >= 5:
        if facts.unoccupied:
            lines = [_t(language, "briefing.tonight_warmer_away", t=format_temp(c.tomorrow_high, temp_unit))]
        else:
            lines = [_t(language, "briefing.tonight_warmer_home", t=format_temp(c.tomorrow_high, temp_unit))]
    elif c.trend_direction == "cooling" and c.trend_magnitude >= 5:
        lines = [_t(language, "briefing.tonight_cooler", t=format_temp(c.tomorrow_high, temp_unit))]
    else:
        lines = [_t(language, "briefing.tonight_similar", t=format_temp(c.tomorrow_high, temp_unit))]
    if adaptive_thermal_active and c.hvac_mode in ("heat", "cool") and not facts.unoccupied:
        lines.append(_t(language, "briefing.tonight_adaptive_tuned"))
    return lines
