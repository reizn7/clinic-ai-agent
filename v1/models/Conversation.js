const mongoose = require("mongoose");
const { CONVERSATION_STATES } = require("../constants");

const conversationSchema = new mongoose.Schema(
    {
        // One conversation per WhatsApp number.
        phone: {
            type: String,
            required: true,
            unique: true,
            trim: true,
            index: true,
        },
        currentState: {
            type: String,
            enum: Object.values(CONVERSATION_STATES),
            default: CONVERSATION_STATES.WELCOME,
        },
        // Scratch space for an in-progress booking (name, age, chosen
        // doctor, the option lists last shown to the user, etc.).
        // Mixed type because its shape evolves per state and a future
        // AI engine may store arbitrary context here.
        tempData: {
            type: mongoose.Schema.Types.Mixed,
            default: {},
        },
    },
    { timestamps: true } // updatedAt is maintained automatically
);

module.exports = mongoose.model("Conversation", conversationSchema);
