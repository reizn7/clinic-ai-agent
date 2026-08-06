const { CONVERSATION_STATES, GENDERS } = require("../../../constants");
const msg = require("../messages");

/**
 * State machine handlers — one entry per state, no cross-state if/else.
 *
 * Each handler receives:
 *   { text, tempData, phone, context }
 * and returns:
 *   { reply, nextState, tempData }
 *
 * `tempData` is the accumulating booking scratchpad. Handlers treat it
 * immutably (spread + return a new object). The engine persists whatever
 * is returned.
 */

const S = CONVERSATION_STATES;

const handlers = {
    // Entry point: greet and show the menu.
    [S.WELCOME]: async ({ context }) => ({
        reply: msg.mainMenu(context.clinic),
        nextState: S.MAIN_MENU,
        tempData: {},
    }),

    [S.MAIN_MENU]: async ({ text, context }) => {
        const choice = String(text).trim();

        if (choice === "1") {
            return {
                reply: "Let's book your appointment.\n\nPlease enter your full name.",
                nextState: S.BOOK_NAME,
                tempData: {}, // start a fresh booking
            };
        }
        if (choice === "2") {
            return {
                reply: msg.clinicTimings(context.clinic),
                nextState: S.MAIN_MENU,
                tempData: {},
            };
        }
        if (choice === "3") {
            return {
                reply: msg.contactReception(context.clinic),
                nextState: S.MAIN_MENU,
                tempData: {},
            };
        }

        // Anything else (including greetings) re-shows the menu.
        return {
            reply: msg.mainMenu(context.clinic),
            nextState: S.MAIN_MENU,
            tempData: {},
        };
    },

    [S.BOOK_NAME]: async ({ text, tempData }) => {
        const name = String(text).trim();
        if (name.length < 2) {
            return {
                reply: "Please enter a valid name (at least 2 characters).",
                nextState: S.BOOK_NAME,
                tempData,
            };
        }
        return {
            reply: `Thanks, ${name}. Please enter your age.`,
            nextState: S.BOOK_AGE,
            tempData: { ...tempData, name },
        };
    },

    [S.BOOK_AGE]: async ({ text, tempData }) => {
        const age = parseInt(String(text).trim(), 10);
        if (Number.isNaN(age) || age < 0 || age > 120) {
            return {
                reply: "Please enter a valid age (a number between 0 and 120).",
                nextState: S.BOOK_AGE,
                tempData,
            };
        }
        return {
            reply: "Please select your gender:\n1. Male\n2. Female\n3. Other",
            nextState: S.BOOK_GENDER,
            tempData: { ...tempData, age },
        };
    },

    [S.BOOK_GENDER]: async ({ text, tempData, context }) => {
        const genderMap = {
            1: GENDERS.MALE,
            2: GENDERS.FEMALE,
            3: GENDERS.OTHER,
            male: GENDERS.MALE,
            female: GENDERS.FEMALE,
            other: GENDERS.OTHER,
        };
        const key = String(text).trim().toLowerCase();
        const gender = genderMap[key];
        if (!gender) {
            return {
                reply: "Please reply 1 for Male, 2 for Female, or 3 for Other.",
                nextState: S.BOOK_GENDER,
                tempData,
            };
        }

        const doctors = await context.getActiveDoctors();
        if (!doctors.length) {
            return {
                reply:
                    "Sorry, no doctors are available right now. Please try again later.\n\n" +
                    msg.mainMenu(context.clinic),
                nextState: S.MAIN_MENU,
                tempData: {},
            };
        }

        const doctorOptions = doctors.map((d) => ({
            id: String(d._id),
            name: d.name,
            specialization: d.specialization,
        }));

        return {
            reply: msg.doctorList(doctorOptions),
            nextState: S.BOOK_DOCTOR,
            tempData: { ...tempData, gender, doctorOptions },
        };
    },

    [S.BOOK_DOCTOR]: async ({ text, tempData, context }) => {
        const options = tempData.doctorOptions || [];
        const idx = msg.parseSelection(text, options.length);
        if (idx === null) {
            return {
                reply: "Please reply with a valid doctor number from the list.",
                nextState: S.BOOK_DOCTOR,
                tempData,
            };
        }

        const chosen = options[idx];
        const doctor = await context.getDoctorById(chosen.id);
        if (!doctor) {
            return {
                reply:
                    "That doctor is no longer available. Please pick another.\n\n" +
                    msg.doctorList(options),
                nextState: S.BOOK_DOCTOR,
                tempData,
            };
        }

        const dateOptions = context.getUpcomingDates(doctor, 5);
        if (!dateOptions.length) {
            return {
                reply:
                    `Dr. ${doctor.name} has no upcoming availability. Please choose another doctor.\n\n` +
                    msg.doctorList(options),
                nextState: S.BOOK_DOCTOR,
                tempData,
            };
        }

        return {
            reply: msg.dateList(dateOptions),
            nextState: S.BOOK_DATE,
            tempData: {
                ...tempData,
                doctorId: chosen.id,
                doctorName: chosen.name,
                dateOptions,
            },
        };
    },

    [S.BOOK_DATE]: async ({ text, tempData, context }) => {
        const options = tempData.dateOptions || [];
        const idx = msg.parseSelection(text, options.length);
        if (idx === null) {
            return {
                reply: "Please reply with a valid date number from the list.",
                nextState: S.BOOK_DATE,
                tempData,
            };
        }

        const date = options[idx];
        const doctor = await context.getDoctorById(tempData.doctorId);
        if (!doctor) {
            return {
                reply:
                    "That doctor is no longer available. Please start again.\n\n" +
                    msg.mainMenu(context.clinic),
                nextState: S.MAIN_MENU,
                tempData: {},
            };
        }

        const slots = await context.getAvailableSlots(doctor, date);
        if (!slots.length) {
            return {
                reply:
                    `No free slots on ${date}. Please choose another date.\n\n` +
                    msg.dateList(options),
                nextState: S.BOOK_DATE,
                tempData,
            };
        }

        return {
            reply: msg.slotList(slots),
            nextState: S.BOOK_SLOT,
            tempData: { ...tempData, date, slotOptions: slots },
        };
    },

    [S.BOOK_SLOT]: async ({ text, tempData }) => {
        const options = tempData.slotOptions || [];
        const idx = msg.parseSelection(text, options.length);
        if (idx === null) {
            return {
                reply: "Please reply with a valid slot number from the list.",
                nextState: S.BOOK_SLOT,
                tempData,
            };
        }

        const slot = options[idx];
        const summary = {
            name: tempData.name,
            age: tempData.age,
            gender: tempData.gender,
            doctorName: tempData.doctorName,
            date: tempData.date,
            slot,
        };

        return {
            reply: msg.confirmation(summary),
            nextState: S.BOOK_CONFIRM,
            tempData: { ...tempData, slot },
        };
    },

    [S.BOOK_CONFIRM]: async ({ text, tempData, phone, context }) => {
        const choice = String(text).trim().toLowerCase();

        // Cancel
        if (choice === "2" || choice === "no" || choice === "cancel") {
            return {
                reply:
                    "No problem, your booking was cancelled.\n\n" +
                    msg.mainMenu(context.clinic),
                nextState: S.MAIN_MENU,
                tempData: {},
            };
        }

        // Confirm
        if (choice === "1" || choice === "yes" || choice === "confirm") {
            try {
                await context.createBooking({
                    phone,
                    name: tempData.name,
                    age: tempData.age,
                    gender: tempData.gender,
                    doctorId: tempData.doctorId,
                    date: tempData.date,
                    slot: tempData.slot,
                });

                return {
                    reply: msg.bookingSuccess({
                        doctorName: tempData.doctorName,
                        date: tempData.date,
                        slot: tempData.slot,
                    }),
                    nextState: S.FINISHED,
                    tempData: {},
                };
            } catch (err) {
                // Slot taken concurrently -> re-offer slots for the same date.
                if (err && err.statusCode === 409) {
                    const doctor = await context.getDoctorById(tempData.doctorId);
                    const slots = doctor
                        ? await context.getAvailableSlots(doctor, tempData.date)
                        : [];
                    if (slots.length) {
                        return {
                            reply: `${err.message}\n\n${msg.slotList(slots)}`,
                            nextState: S.BOOK_SLOT,
                            tempData: { ...tempData, slotOptions: slots },
                        };
                    }
                    return {
                        reply:
                            `${err.message}\n\n` + msg.mainMenu(context.clinic),
                        nextState: S.MAIN_MENU,
                        tempData: {},
                    };
                }
                throw err;
            }
        }

        // Unrecognised confirm input
        return {
            reply: "Please reply 1 to Confirm or 2 to Cancel.",
            nextState: S.BOOK_CONFIRM,
            tempData,
        };
    },

    // Terminal state: any new message restarts the flow.
    [S.FINISHED]: async ({ context }) => ({
        reply: msg.mainMenu(context.clinic),
        nextState: S.MAIN_MENU,
        tempData: {},
    }),
};

module.exports = handlers;
