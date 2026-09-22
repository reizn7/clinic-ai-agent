/**
 * AI conversation engine — EXTENSION POINT (not wired in yet).
 *
 * This is a placeholder that documents the contract an AI-powered engine
 * must satisfy so it can DROP-IN replace the rule engine with zero changes
 * elsewhere. The AI must NOT know anything about WhatsApp.
 *
 * To activate later:
 *   1. Implement `process` below using OPENAI_API_KEY (already in .env).
 *   2. In services/conversationService.js change:
 *        const engine = require("./engine/ruleEngine");
 *      to:
 *        const engine = require("./engine/aiEngine");
 *   Nothing else in the app changes.
 *
 * The engine receives exactly:
 *   - conversation : { phone, currentState, tempData }   (Current Conversation)
 *   - message      : incoming text                       (Message)
 *   - context      : engine/contextBuilder.js helpers    (Database Context)
 * and must return:
 *   - { reply, nextState, tempData }                     (Reply + Next State)
 */

// eslint-disable-next-line no-unused-vars
const process = async ({ conversation, message, context }) => {
    throw new Error(
        "aiEngine.process() not implemented yet. Use ruleEngine for now."
    );
};

module.exports = { process };
