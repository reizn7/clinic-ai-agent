const { weekdayName } = require("../../utils/dateUtils");

/**
 * Pure text builders + input parsers for the rule engine.
 * No DB, no side effects — easy to unit test and reuse.
 */

const mainMenu = (clinic) =>
    `Welcome to ${clinic.clinicName} 👋\n\n` +
    `1. Book Appointment\n` +
    `2. Clinic Timings\n` +
    `3. Contact Reception\n\n` +
    `Reply with a number.`;

const clinicTimings = (clinic) =>
    `🕒 *${clinic.clinicName} Timings*\n${clinic.timings || "Not available."}\n\n` +
    `Reply with a number:\n1. Book Appointment\n2. Clinic Timings\n3. Contact Reception`;

const contactReception = (clinic) =>
    `📞 *Contact Reception*\n` +
    `${clinic.phone || "Phone not available."}\n` +
    `${clinic.address || ""}\n\n` +
    `Reply with a number:\n1. Book Appointment\n2. Clinic Timings\n3. Contact Reception`;

/** Numbered list of doctors. `options` is [{ name, specialization }]. */
const doctorList = (options) => {
    const lines = options
        .map((d, i) => `${i + 1}. Dr. ${d.name} (${d.specialization})`)
        .join("\n");
    return `Please select a doctor:\n${lines}\n\nReply with a number.`;
};

/** Numbered list of date keys ("YYYY-MM-DD"), annotated with weekday. */
const dateList = (dateKeys) => {
    const lines = dateKeys
        .map((d, i) => `${i + 1}. ${d} (${weekdayName(d)})`)
        .join("\n");
    return `Please select a date:\n${lines}\n\nReply with a number.`;
};

/** Numbered list of slot start times ("HH:mm"). */
const slotList = (slots) => {
    const lines = slots.map((s, i) => `${i + 1}. ${s}`).join("\n");
    return `Please select a time slot:\n${lines}\n\nReply with a number.`;
};

const confirmation = ({ name, age, gender, doctorName, date, slot }) =>
    `Please confirm your appointment:\n\n` +
    `👤 Name: ${name}\n` +
    `🎂 Age: ${age}\n` +
    `⚧ Gender: ${gender}\n` +
    `🩺 Doctor: Dr. ${doctorName}\n` +
    `📅 Date: ${date} (${weekdayName(date)})\n` +
    `⏰ Slot: ${slot}\n\n` +
    `Reply 1 to Confirm, 2 to Cancel.`;

const bookingSuccess = ({ doctorName, date, slot }) =>
    `✅ Appointment booked successfully!\n\n` +
    `Dr. ${doctorName}\n${date} (${weekdayName(date)}) at ${slot}\n\n` +
    `Status: Pending confirmation.\nSend "Hi" anytime to start again.`;

// ---- input parsing ----

/** Parse a 1-based menu selection into a 0-based index within `length`. */
const parseSelection = (text, length) => {
    const n = parseInt(String(text).trim(), 10);
    if (Number.isNaN(n) || n < 1 || n > length) return null;
    return n - 1;
};

const isGreeting = (text) =>
    /^(hi|hello|hey|start|menu|hola|namaste)\b/i.test(String(text).trim());

module.exports = {
    mainMenu,
    clinicTimings,
    contactReception,
    doctorList,
    dateList,
    slotList,
    confirmation,
    bookingSuccess,
    parseSelection,
    isGreeting,
};
