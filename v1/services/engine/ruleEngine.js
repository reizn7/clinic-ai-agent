const { CONVERSATION_STATES } = require("../../constants");
const handlers = require("./states");

/**
 * Rule-based conversation engine.
 *
 * THE ENGINE CONTRACT (implement this same shape for the future AI engine):
 *
 *   process({ conversation, message, context }) -> { reply, nextState, tempData }
 *
 *   - conversation : { phone, currentState, tempData }
 *   - message      : the raw incoming text
 *   - context      : database context (see engine/contextBuilder.js)
 *
 * The engine is pure with respect to delivery — it has no idea the reply
 * will be sent over WhatsApp. To plug in AI later, create e.g.
 * services/engine/aiEngine.js exposing the same `process` function and
 * swap the require in conversationService.js. Nothing else changes.
 */
const process = async ({ conversation, message, context }) => {
    const currentState =
        conversation.currentState || CONVERSATION_STATES.WELCOME;

    // Unknown/legacy state -> restart safely from WELCOME.
    const handler = handlers[currentState] || handlers[CONVERSATION_STATES.WELCOME];

    const result = await handler({
        text: message,
        tempData: conversation.tempData || {},
        phone: conversation.phone,
        context,
    });

    return {
        reply: result.reply,
        nextState: result.nextState,
        tempData: result.tempData !== undefined ? result.tempData : conversation.tempData,
    };
};

module.exports = { process };
