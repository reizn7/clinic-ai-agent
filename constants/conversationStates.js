/**
 * Conversation state machine states.
 *
 * These are the ONLY valid values for `Conversation.currentState`.
 * The rule-based engine (services/engine/ruleEngine.js) transitions
 * between these states. A future AI engine can reuse the exact same
 * set of states, so keep this list authoritative.
 */
const CONVERSATION_STATES = Object.freeze({
    WELCOME: "WELCOME",
    MAIN_MENU: "MAIN_MENU",
    BOOK_NAME: "BOOK_NAME",
    BOOK_AGE: "BOOK_AGE",
    BOOK_GENDER: "BOOK_GENDER",
    BOOK_DOCTOR: "BOOK_DOCTOR",
    BOOK_DATE: "BOOK_DATE",
    BOOK_SLOT: "BOOK_SLOT",
    BOOK_CONFIRM: "BOOK_CONFIRM",
    FINISHED: "FINISHED",
});

module.exports = { CONVERSATION_STATES };
