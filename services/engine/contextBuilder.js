const clinicService = require("../clinicService");
const doctorService = require("../doctorService");
const appointmentService = require("../appointmentService");
const patientService = require("../patientService");
const { upcomingDatesForWeekdays } = require("../../utils/dateUtils");

/**
 * Builds the "Database Context" handed to a conversation engine.
 *
 * This is the seam that keeps the engine (rule-based today, AI tomorrow)
 * completely decoupled from persistence AND from WhatsApp. An engine only
 * ever sees: the current conversation, the incoming message, and this
 * context object of data-access helpers. It returns { reply, nextState,
 * tempData } and knows nothing about how messages are delivered.
 */
const buildContext = async () => {
    const clinic = await clinicService.getClinic();

    return {
        clinic,

        getActiveDoctors: () => doctorService.listActive(),

        getDoctorById: (id) => doctorService.findById(id),

        getUpcomingDates: (doctor, count = 5) =>
            upcomingDatesForWeekdays(doctor.availableDays, count),

        getAvailableSlots: (doctor, date) =>
            appointmentService.getAvailableSlotsForDoctor(doctor, date),

        /**
         * Persist a completed booking: upsert the patient by phone, then
         * create the appointment. Returns { patient, appointment }.
         */
        createBooking: async ({ phone, name, age, gender, doctorId, date, slot }) => {
            const patient = await patientService.upsertByPhone(phone, {
                name,
                age,
                gender,
            });
            const appointment = await appointmentService.create({
                patientId: patient._id,
                doctorId,
                date,
                slot,
            });
            return { patient, appointment };
        },
    };
};

module.exports = { buildContext };
