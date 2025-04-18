import os
import time
from typing import Dict, Any, List, Optional, Union, ClassVar, Type
import logging
from llama_index.core import Settings
from llama_index.core.workflow import Workflow, Context, Event, step, StopEvent, StartEvent
from llama_index.llms.openai import OpenAI
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.core.base.llms.types import MessageRole, ChatMessage
from utils import menu_to_string
import json

# Set up logger
logger = logging.getLogger("food_ordering_bot")

# Define custom event classes
class ResponseEvent(Event):
    """Event containing the response content and action type."""
    response: str
    action_type: str
    original_query: Optional[str] = None # Add field to carry original query
    cart_items: Optional[List[Dict[str, Any]]] = None # Add field for cart items

class ChatResponseStopEvent(StopEvent):
    """Custom StopEvent with response and action_type fields."""
    response: str
    action_type: str
    cart_items: Optional[List[Dict[str, Any]]] = None # Add field for cart items

class FoodOrderingWorkflow(Workflow):
    """
    A workflow that implements the food ordering chatbot conversation logic.
    """
    # Define the start event class
    start_event_cls: ClassVar[Type[Event]] = StartEvent
    
    def __init__(self, menu: Dict[str, Dict[str, Any]], chat_history: List[ChatMessage] = None, timeout: float = 60.0):
        # Configure LLM settings - do this before calling super().__init__()
        Settings.llm = OpenAI(model="gpt-4o", temperature=0.7, request_timeout=30)
        Settings.embed_model = OpenAIEmbedding(model="text-embedding-ada-002")
        
        # Call super().__init__() with explicit timeout
        super().__init__(timeout=timeout)
        
        # Set instance attributes
        self.menu = menu
        self.chat_history = chat_history or []
        self.menu_text = menu_to_string(menu)
        logger.info(f"FoodOrderingWorkflow initialized with timeout={timeout}")
    
    @step
    async def classify_and_respond(self, ctx: Context, ev: StartEvent) -> Union[ResponseEvent, ChatResponseStopEvent]:
        """
        First step: Classify intent. 
        If menu/order, return ResponseEvent with pending action type.
        If greeting/end/irrelevant, return ChatResponseStopEvent directly to end workflow.
        """
        # Get the query from the start event
        query = ev.content # Correct way to access StartEvent content
        logger.info(f"Processing query: '{query}'")

        # Format chat history for the prompt
        formatted_history = "\n".join([
            f"{'USER' if msg.role == MessageRole.USER else 'ASSISTANT'}: {msg.content}"
            for msg in self.chat_history
        ]) if self.chat_history else "No conversation history yet."

        # Determine intent with prompt, now including history
        router_prompt = f"""
        You are a restaurant chatbot assistant classifying user intent.
        
        **ABSOLUTE RULES - APPLY THESE FIRST:**
        1.  If the user message is a simple, standalone greeting (e.g., "hello", "hi"), the intent is GREETING.
        2.  If the user message is asking *about the conversation itself* (e.g., "what did I ask?", "what was my last message?", "what did we talk about?"), the intent is HISTORY, regardless of the topic of previous messages.

        **Conversation History:**
        {formatted_history}
        
        **Current User Message:** "{query}"
        
        Given the user message, conversation history, and the absolute rules, classify the intent into ONE of the following:
        - GREETING: A simple greeting. **Must follow Absolute Rule 1.**
        - HISTORY: A question *about* the conversation history or previous messages/orders. **Must follow Absolute Rule 2.**
        - MENU: Asking about menu items/prices/descriptions. *Excludes questions about what was previously discussed.*
        - ORDER: Requesting to create, modify, review, or cancel an order. *Excludes questions about previous orders already discussed.*
        - END: Ending the conversation (e.g., "bye", "thank you").
        - IRRELEVANT: Any other topic not covered above.
        
        Output Instructions:
        1. Return STRICTLY a JSON object with keys "intent" and "response".
        2. For GREETING or HISTORY intents: Set the correct "intent". Provide a **concise** response text (under 320 characters) in the "response" field. Use the provided Conversation History context to answer HISTORY questions accurately. Summarize if necessary and offer to provide more detail if relevant.
        3. For IRRELEVANT intent: Set "intent" to "IRRELEVANT". Provide a **concise**, empathetic refusal/explanation (under 320 characters) in the "response" field.
        4. For MENU, ORDER, or END intents: Set the correct "intent" and set "response" to an empty string ("").
        
        Examples (History examples assume relevant context was in the provided history):
        User message: "hello"
        Output: {{"intent": "GREETING", "response": "Hello! How can I help with the menu or your order?"}} # Concise

        User message: "What drinks do you have?"
        Output: {{"intent": "MENU", "response": ""}}

        User message: "what did I ask before this?"
        Output: {{"intent": "HISTORY", "response": "You previously asked about our sandwich options. Need more details on those, or can I help with something else?"}} # Concise, offers detail

        User message: "What was my previous message?"
        Output: {{"intent": "HISTORY", "response": "Your previous message was asking about drinks. Anything else I can help with?"}} # Concise summary

        User message: "what was the first thing I asked?"
        Output: {{"intent": "HISTORY", "response": "Looks like your first message was 'Hello'. How can I help now?"}} # Concise answer

        User message: "what did I order last time?"
        Output: {{"intent": "HISTORY", "response": "We discussed you ordering pizza previously. Want to order that now or see the menu again?"}} # Concise summary

        User message: "tell me a joke"
        Output: {{"intent": "IRRELEVANT", "response": "Sorry, I can't tell jokes! I'm here for menu questions or orders. Can I help with that?"}} # Concise

        User message: "thanks bye"
        Output: {{"intent": "END", "response": ""}}

        Ensure no extra text before or after the JSON object.
        """

        llm = OpenAI(model="gpt-4o", temperature=0.0, request_timeout=30)
        logger.info("Sending intent classification request to OpenAI")
        
        # Measure response time for intent classification
        start_time = time.time()
        router_response = await llm.acomplete(router_prompt)
        elapsed = time.time() - start_time
        
        # Parse the JSON response
        response_text = router_response.text.strip()
        logger.info(f"Router response (took {elapsed:.2f}s): {response_text}")
        
        # Extract intent and direct response from JSON
        intent = ""
        direct_response = "" # Renamed from general_response for clarity
        
        try:
            # Parse the JSON response
            response_data = json.loads(response_text)
            
            # Extract the fields with better validation
            if isinstance(response_data, dict):
                intent = response_data.get("intent", "").lower() if response_data.get("intent") else ""
                direct_response = response_data.get("response", "") if response_data.get("response") else ""
                
                # Validate the extracted data
                valid_intents = ["menu", "order", "greeting", "end", "irrelevant", "history"]
                if not intent or intent not in valid_intents:
                    # Default to irrelevant if intent is invalid or missing
                    logger.warning(f"Invalid or missing intent '{intent}', defaulting to 'irrelevant'")
                    intent = "irrelevant"
                    # Ensure we provide a default refusal if the intent was bad AND no response was given
                    if not direct_response:
                         direct_response = "I'm sorry, I encountered an issue. I can only assist with menu questions and food orders."

                # If intent is GREETING, IRRELEVANT, or HISTORY, check response validity
                if intent in ["greeting", "irrelevant", "history"]:
                   if not isinstance(direct_response, str) or not direct_response.strip():
                       logger.warning(f"Invalid or empty direct response for intent '{intent}': '{direct_response}'. Using fallback message.")
                       # Provide a default response based on the (potentially defaulted) intent
                       if intent == "greeting":
                            direct_response = "Hello! How can I help with the menu or your order?"
                       elif intent == "history":
                            direct_response = "I can see you're asking about our previous conversation. How can I help you with our menu or placing an order?"
                       else: # irrelevant
                            direct_response = "I'm sorry, I can only assist with questions about our menu and help you place an order."
                   elif direct_response.strip() in ['{', '}', '[]', '[', ']', '{}', ':', '""', "''", ',', '.']:
                       logger.warning(f"Direct response for intent '{intent}' looks like a fragment: '{direct_response}'. Using fallback message.")
                       if intent == "greeting":
                            direct_response = "Hello! How can I help with the menu or your order?"
                       elif intent == "history":
                            direct_response = "I can see you're asking about our previous conversation. How can I help you with our menu or placing an order?"
                       else: # irrelevant
                            direct_response = "I'm sorry, I can only assist with questions about our menu and help you place an order."
                
                logger.info(f"Successfully parsed JSON response: intent='{intent}', response_length={len(direct_response)}")
            else:
                logger.error("Response data is not a dictionary, defaulting to irrelevant")
                intent = "irrelevant"
                direct_response = "I'm sorry, I encountered an issue processing the response. I can only assist with menu questions and food orders."
        except json.JSONDecodeError as e:
            # Fallback if JSON parsing fails
            logger.error(f"Failed to parse JSON response: {e}")
            logger.info(f"Raw response text was: {response_text}")
            logger.info("Attempting keyword-based intent extraction as fallback...")
            
            # Simple keyword check on the raw text
            raw_text_lower = response_text.lower()
            if "menu" in raw_text_lower:
                intent = "menu"
                logger.info("Fallback: Detected 'menu' keyword.")
            elif "order" in raw_text_lower or "cart" in raw_text_lower or "checkout" in raw_text_lower:
                intent = "order"
                logger.info("Fallback: Detected 'order/cart/checkout' keyword.")
            elif any(greeting in raw_text_lower for greeting in ["hello", "hi ", " how are"]):
                 intent = "greeting"
                 direct_response = "Hello! How can I help you with the menu or your order today?" # Provide default greeting
                 logger.info("Fallback: Detected greeting keyword.")
            elif any(farewell in raw_text_lower for farewell in ["bye", "thank you", "thanks"]):
                 intent = "end"
                 logger.info("Fallback: Detected farewell keyword.")
            else:
                # Only default to irrelevant if no keywords match
                intent = "irrelevant"
                direct_response = "I'm sorry, I had trouble understanding that. I can only assist with menu questions and food orders." # Specific message for this fallback path
                logger.info("Fallback: No relevant keywords detected, defaulting to irrelevant.")
                
        logger.info(f"Intent classified as: '{intent}' (took {elapsed:.2f}s)")
        
        # Generate appropriate response or acknowledgment based on intent
        response = ""
        action_type = ""
        result = None

        try:
            if intent == "menu":
                logger.info("Intent: MENU. Returning acknowledgment.")
                response = "Give us a moment while we research that for you."
                action_type = "menu_inquiry_pending" # Temporary type
                result = ResponseEvent(
                    response=response,
                    action_type=action_type,
                    original_query=query # Pass query for next step
                )
            elif intent == "order":
                logger.info("Intent: ORDER. Returning acknowledgment.")
                # Basic check for modification keywords - can be enhanced
                if any(kw in query.lower() for kw in ["change", "modify", "add", "remove", "update"]):
                     response = "Give us a moment while we get that order modification ready for you."
                else:
                     response = "Give us a moment while we get that order ready for you."
                action_type = "order_action_pending" # Temporary type
                result = ResponseEvent(
                    response=response,
                    action_type=action_type,
                    original_query=query # Pass query for next step
                )
            elif intent == "greeting":
                logger.info("Handling greeting directly")
                response = direct_response
                action_type = "greeting"
                # Return a ChatResponseStopEvent directly for greeting
                result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type
                )
            elif intent == "end":
                logger.info("Handling end conversation")
                # Call _handle_end_conversation directly as it's simple
                response = await self._handle_end_conversation(query)
                action_type = "end_conversation"
                # Return a ChatResponseStopEvent directly for end
                result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type
                )
            elif intent == "history":
                logger.info("Handling history query directly")
                response = direct_response
                action_type = "history_query"
                # Return a ChatResponseStopEvent directly for history
                result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type
                )
            elif intent == "irrelevant":
                 logger.info("Handling irrelevant query directly")
                 response = direct_response
                 action_type = "irrelevant_query"
                 # Return a ChatResponseStopEvent directly for irrelevant
                 result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type
                 )
            else: # Should not happen due to validation, but good to have a fallback
                logger.error(f"Reached unexpected else block for intent: {intent}")
                response = "I'm sorry, I'm not sure how to handle that. I can assist with menu questions and orders."
                action_type = "error"
                # Return a ChatResponseStopEvent directly for error
                result = ChatResponseStopEvent(
                    result=None,  # Required by StopEvent
                    response=response,
                    action_type=action_type
                )

            logger.info(f"Step 1 Result: Type='{action_type}', Response='{response[:50]}...'")
            return result
            
        except Exception as e:
            logger.error(f"Error in classify_and_respond logic block: {type(e).__name__}: {str(e)}")
            # Provide a generic refusal in case of errors in handlers
            return ChatResponseStopEvent(
                result=None,  # Required by StopEvent
                response="I'm sorry, I encountered an error and cannot process your request. I can only assist with menu questions and food orders.",
                action_type="error"
            )
    
    @step
    async def generate_detailed_response(self, ctx: Context, ev: ResponseEvent) -> ResponseEvent:
        """
        Second step: If the previous step returned a pending action type,
        generate the detailed response using the appropriate handler.
        Otherwise, pass the event through by creating a new event object.
        """
        logger.info(f"Entering generate_detailed_response with action_type: {ev.action_type}")
        
        if ev.action_type == "menu_inquiry_pending":
            logger.info("Handling pending menu inquiry")
            if ev.original_query:
                response_text = await self._handle_menu_query(ev.original_query)
                logger.info(f"Generated detailed menu response: {response_text[:50]}...")
                return ResponseEvent(
                    response=response_text,
                    action_type="menu_inquiry", # Final action type
                    cart_items=None # No cart changes for menu inquiries
                )
            else:
                logger.error("Original query missing for menu_inquiry_pending")
                return ResponseEvent(response="Error: Missing query for menu info.", action_type="error", cart_items=None)
                
        elif ev.action_type == "order_action_pending":
            logger.info("Handling pending order action")
            if ev.original_query:
                response_text, cart_items = await self._handle_order_query(ev.original_query)
                logger.info(f"Generated detailed order response: {response_text[:50]}...")
                return ResponseEvent(
                    response=response_text,
                    action_type="order_action", # Final action type
                    cart_items=cart_items # Include cart items
                )
            else:
                 logger.error("Original query missing for order_action_pending")
                 return ResponseEvent(response="Error: Missing query for order action.", action_type="error", cart_items=None)
                 
        else:
            # If action type is already final (greeting, end, irrelevant, error, history), pass through
            # Explicitly create a new event object to avoid potential issues with object identity.
            logger.info(f"Passing through response with final action_type: {ev.action_type} by creating new event")
            return ResponseEvent(
                response=ev.response,
                action_type=ev.action_type,
                original_query=ev.original_query, # Ensure all relevant fields are copied
                cart_items=None # No cart changes for non-order actions
            )

    @step
    async def finalize(self, ctx: Context, ev: ResponseEvent) -> ChatResponseStopEvent:
        """
        Final step: Convert ResponseEvent to ChatResponseStopEvent
        """
        logger.info(f"Finalizing response: {ev.response[:30]}...")
        # Create our custom ChatResponseStopEvent with proper fields
        result = ChatResponseStopEvent(
            # Set 'result' to None (required by StopEvent) 
            result=None,
            # Add our custom fields
            response=ev.response,
            action_type=ev.action_type,
            cart_items=ev.cart_items # Pass through cart items
        )
        logger.info(f"Created ChatResponseStopEvent with fields: response={result.response[:20]}..., action_type={result.action_type}")
        return result
    
    async def _handle_menu_query(self, query: str) -> str:
        """Handle menu-related queries"""
        menu_template = f"""
        You are a helpful restaurant assistant providing information about menu items.
        Respond concisely, like a text message (under 320 characters).
        Summarize information where possible, especially if the user asks for general categories or multiple items.
        **Use the provided conversation history to understand the context and avoid repeating information unnecessarily.**
        
        **Handling Follow-up for "More Details":**
        - If the user asks for "more details" after you've provided a summary, look at your *immediately preceding message* in the history.
        - Identify the items you summarized in that message.
        - Provide the *additional* details (like descriptions, options, ingredients) for *only those items*.
        - Do NOT repeat the item names and prices from the summary unless essential for context (e.g., listing options with price modifiers).
        - Keep the response concise and under the character limit.
        
        Be friendly and informative about prices and descriptions.
        Use standard text formatting. Avoid complex markdown. Use bold (**) for item names only.
        The complete menu is as follows:
        {self.menu_text}
        """
        
        # Generate response
        messages = self.chat_history + [
            ChatMessage(role=MessageRole.USER, content=query),
            ChatMessage(role=MessageRole.SYSTEM, content=menu_template)
        ]
        
        logger.info("_handle_menu_query: Sending request to OpenAI")
        llm = OpenAI(model="gpt-4o", temperature=0.7, request_timeout=30)
        try:
            # Measure response time for menu query
            start_time = time.time()
            response = await llm.achat(messages)
            elapsed = time.time() - start_time
            
            logger.info(f"_handle_menu_query: Got response in {elapsed:.2f}s")
            return response.message.content
        except Exception as e:
            logger.error(f"_handle_menu_query: Error: {type(e).__name__}: {str(e)}")
            return f"I'm sorry, I had trouble providing menu information. Error: {str(e)}"
    
    async def _handle_order_query(self, query: str) -> str:
        """Handle order-related actions"""
        order_template = f"""
        You are an assistant helping with food orders.
        Respond concisely, like a text message (under 320 characters).
        **Use the provided conversation history to understand the current order status and context.**
        Be clear about prices and options. Summarize complex orders or options if necessary.
        Offer to provide more detail if needed.
        If they want something not on the menu, politely inform them it's unavailable.
        
        In addition to your text response, you must also manage and return the user's cart state.
        You need to parse the user's intent and:
        1. For "add" - add items to the cart
        2. For "remove" - remove items from the cart
        3. For "change" - modify existing items (e.g., change quantity, options)
        4. For "upgrade" - upgrade items (e.g., size, add-ons)
        5. For "cancel order" - empty the cart
        6. For "make order" - finalize the cart
        
        When responding, output BOTH:
        1. A conversational text message acknowledging the user's action
        2. A valid JSON representation of their updated cart
        
        The cart should be a JSON array of objects with properties:
        - "item": string - the menu item name
        - "quantity": number - how many of this item
        - "options": array of strings - any options/modifications
        - "price": number - the unit price of this item including options
        
        FORMAT:
        {{
          "response": "Your natural language response here",
          "cart": []
        }}
        
        Based on the menu information and the user's request, help them place or modify their order.
        The complete menu is as follows:
        {self.menu_text}
        """
        
        # Generate response
        messages = self.chat_history + [
            ChatMessage(role=MessageRole.USER, content=query),
            ChatMessage(role=MessageRole.SYSTEM, content=order_template)
        ]
        
        logger.info("_handle_order_query: Sending request to OpenAI")
        llm = OpenAI(model="gpt-4o", temperature=0.7, request_timeout=30)
        try:
            # Measure response time for order query
            start_time = time.time()
            response = await llm.achat(messages)
            elapsed = time.time() - start_time
            
            logger.info(f"_handle_order_query: Got response in {elapsed:.2f}s")
            
            # Parse response to extract cart information
            response_content = response.message.content
            cart_items = []
            
            try:
                # Try to parse the response as JSON
                if isinstance(response_content, str):
                    # Extract JSON object using regex for better reliability
                    import re
                    # Look for JSON objects in the text
                    json_matches = re.findall(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', response_content, re.DOTALL)
                    
                    if json_matches:
                        # Try each match until we find a valid JSON object with the expected structure
                        for json_str in json_matches:
                            try:
                                data = json.loads(json_str)
                                if isinstance(data, dict) and "response" in data:
                                    # Found a valid JSON object with "response" field
                                    response_content = data.get("response", "")
                                    # Extract cart items if available
                                    if "cart" in data and isinstance(data["cart"], list):
                                        cart_items = data["cart"]
                                        logger.info(f"Extracted cart items: {len(cart_items)} items")
                                    break  # Stop after finding the first valid match
                            except json.JSONDecodeError:
                                continue  # Try the next match
                    else:
                        logger.warning("No JSON objects found in the response")
                else:
                    logger.warning(f"Response content is not a string: {type(response_content)}")
            except Exception as e:
                logger.error(f"Error extracting cart data: {type(e).__name__}: {str(e)}")
                # Continue with original response content
            
            return response_content, cart_items
        except Exception as e:
            logger.error(f"_handle_order_query: Error: {type(e).__name__}: {str(e)}")
            return f"I'm sorry, I had trouble with your order. Error: {str(e)}", []
    
    async def _handle_end_conversation(self, query: str) -> str:
        """Handle end of conversation"""
        end_template = """
        The user seems to be ending the conversation.
        **Consider the conversation history for context if appropriate (e.g., thanking them for an order).**
        Respond with a friendly, concise goodbye message (under 320 characters) that invites them to return.
        """
        
        messages = self.chat_history + [
            ChatMessage(role=MessageRole.USER, content=query),
            ChatMessage(role=MessageRole.SYSTEM, content=end_template)
        ]
        
        logger.info("_handle_end_conversation: Sending request to OpenAI")
        llm = OpenAI(model="gpt-4o", temperature=0.7, request_timeout=30)
        try:
            # Measure response time for end conversation
            start_time = time.time()
            response = await llm.achat(messages)
            elapsed = time.time() - start_time
            
            logger.info(f"_handle_end_conversation: Got response in {elapsed:.2f}s")
            return response.message.content
        except Exception as e:
            logger.error(f"_handle_end_conversation: Error: {type(e).__name__}: {str(e)}")
            return f"I'm sorry, I had trouble saying goodbye. Error: {str(e)}"

def create_chat_engine(menu: Dict[str, Dict[str, Any]], chat_history: List[Dict[str, str]] = None):
    """
    Create a chat workflow that follows the architectural diagram.
    
    Args:
        menu: Dictionary containing menu items
        chat_history: List of previous chat messages
    
    Returns:
        A workflow object that can be used to process chat messages
    """
    formatted_chat_history = []
    if chat_history:
        # Keep only the most recent 20 messages
        recent_history = chat_history[-20:]
        logger.info(f"Using the last {len(recent_history)} messages out of {len(chat_history)} for history.")
        for message in recent_history:
            role = MessageRole.USER if message["role"] == "user" else MessageRole.ASSISTANT
            formatted_chat_history.append(ChatMessage(role=role, content=message["content"]))
    
    # Create and return workflow with an extended timeout (60 seconds)
    workflow = FoodOrderingWorkflow(
        menu=menu, 
        chat_history=formatted_chat_history,
        timeout=60.0  # Increase the timeout to 60 seconds
    )
    
    # The @step decorators automatically register and link the steps based on type hints.
    # Explicit add_step calls are not needed here.
    # workflow.add_step(workflow.classify_and_respond)
    # workflow.add_step(workflow.generate_detailed_response, input_step_name="classify_and_respond")
    # workflow.add_step(workflow.finalize, input_step_name="generate_detailed_response")
    
    return workflow 