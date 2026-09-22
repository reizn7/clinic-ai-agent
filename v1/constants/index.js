const { CONVERSATION_STATES } = require("./conversationStates");
const {
    APPOINTMENT_STATUS,
    APPOINTMENT_STATUS_VALUES,
} = require("./appointmentStatus");

const GENDERS = Object.freeze({
    MALE: "Male",
    FEMALE: "Female",
    OTHER: "Other",
});

const GENDER_VALUES = Object.freeze(Object.values(GENDERS));

// Days of week as stored on Doctor.availableDays / Clinic.timings.
const WEEKDAYS = Object.freeze([
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]);

module.exports = {
    CONVERSATION_STATES,
    APPOINTMENT_STATUS,
    APPOINTMENT_STATUS_VALUES,
    GENDERS,
    GENDER_VALUES,
    WEEKDAYS,
};
