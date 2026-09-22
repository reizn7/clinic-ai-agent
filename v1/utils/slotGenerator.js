/**
 * Slot generation for a doctor's working hours.
 *
 * A "slot" is a "HH:mm" start time. Given working hours and a
 * consultation duration, we tile the day into fixed-length slots.
 */

/** "HH:mm" -> minutes since midnight. */
const toMinutes = (hhmm) => {
    const [h, m] = String(hhmm).split(":").map(Number);
    return h * 60 + m;
};

/** minutes since midnight -> "HH:mm". */
const toHHMM = (minutes) => {
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
};

/**
 * Generate all slot start-times for a working window.
 * @param {{start: string, end: string}} workingHours e.g. { start: "09:00", end: "17:00" }
 * @param {number} durationMinutes consultation length, e.g. 30
 * @returns {string[]} e.g. ["09:00", "09:30", ...]
 */
const generateSlots = (workingHours, durationMinutes) => {
    if (!workingHours || !workingHours.start || !workingHours.end) return [];
    const duration = Number(durationMinutes) || 30;
    const start = toMinutes(workingHours.start);
    const end = toMinutes(workingHours.end);

    const slots = [];
    for (let t = start; t + duration <= end; t += duration) {
        slots.push(toHHMM(t));
    }
    return slots;
};

/**
 * Remove already-booked slots from the generated list.
 * @param {string[]} allSlots
 * @param {string[]} bookedSlots
 */
const availableSlots = (allSlots, bookedSlots = []) => {
    const taken = new Set(bookedSlots);
    return allSlots.filter((s) => !taken.has(s));
};

module.exports = { generateSlots, availableSlots };
