const Conversation = require("../models/Conversation");
const { CONVERSATION_STATES } = require("../constants");
const { buildContext } = require("./engine/contextBuilder");

// The active engine. Swap this single require for ./engine/aiEngine
// (same `process` contract) to make the assistant AI-powered later.
const engine = require("./engine/ruleEngine");

/**
 * Conversation orchestration layer.
 *
 * This is the ONLY thing the webhook needs to call to produce a reply:
 *
 *     const replyText = await conversationService.handleMessage(phone, text);
 *     await whatsappService.sendTextMessage(phone, replyText);
 *
 * Responsibilities:
 *   - load (or create) the persisted Conversation for this phone
 *   - build the database context
 *   - delegate the actual decision to the engine (rule-based / AI)
 *   - persist the resulting state + tempData
 *   - return the reply text (delivery is the caller's concern)
 */
const handleMessage = async (phone, message) => {
    // 1. Load or create the conversation for this WhatsApp number.
    let conversation = await Conversation.findOne({ phone });
    if (!conversation) {
        conversation = await Conversation.create({
            phone,
            currentState: CONVERSATION_STATES.WELCOME,
            tempData: {},
        });
    }

    // 2. Build the database context handed to the engine.
    const context = await buildContext();

    // 3. Delegate to the engine (WhatsApp-agnostic).
    const { reply, nextState, tempData } = await engine.process({
        conversation: {
            phone: conversation.phone,
            currentState: conversation.currentState,
            tempData: conversation.tempData,
        },
        message,
        context,
    });

    // 4. Persist the new state.
    conversation.currentState = nextState;
    conversation.tempData = tempData;
    conversation.markModified("tempData"); // Mixed type needs an explicit flag
    await conversation.save();

    // 5. Return the reply text for the caller to deliver.
    return reply;
};

module.exports = { handleMessage };
