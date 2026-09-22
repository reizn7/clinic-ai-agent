const { WEEKDAYS } = require("../constants");

/**
 * Date helpers built on the native Date object (no external deps).
 *
 * Convention used across the codebase:
 *   - Appointment.date is stored as a "YYYY-MM-DD" string.
 *   - Appointment.slot is stored as a "HH:mm" string.
 * Keeping these as plain strings avoids timezone drift for a
 * single-clinic booking system and is trivial for the dashboard to filter.
 */

/** Format a Date as "YYYY-MM-DD". */
const toDateKey = (date) => {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, "0");
    const d = String(date.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
};

/** Weekday name (e.g. "Monday") for a Date or "YYYY-MM-DD" string. */
const weekdayName = (dateOrKey) => {
    const date =
        dateOrKey instanceof Date ? dateOrKey : new Date(`${dateOrKey}T00:00:00`);
    return WEEKDAYS[date.getDay()];
};

/** Today's date key ("YYYY-MM-DD"). */
const todayKey = () => toDateKey(new Date());

/**
 * Next `count` upcoming date keys (starting tomorrow) whose weekday
 * is included in `availableDays`. Used to offer booking dates.
 */
const upcomingDatesForWeekdays = (availableDays, count = 5) => {
    const allowed = new Set(availableDays || []);
    const results = [];
    const cursor = new Date();
    let guard = 0;

    // Start from tomorrow; scan forward up to ~60 days as a safety bound.
    while (results.length < count && guard < 60) {
        cursor.setDate(cursor.getDate() + 1);
        guard += 1;
        if (allowed.size === 0 || allowed.has(WEEKDAYS[cursor.getDay()])) {
            results.push(toDateKey(new Date(cursor)));
        }
    }
    return results;
};

module.exports = {
    toDateKey,
    weekdayName,
    todayKey,
    upcomingDatesForWeekdays,
};
