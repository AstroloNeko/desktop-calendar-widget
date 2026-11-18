"""Calendar Agent adapters and operation workflow.

Desktop and HTTP use calendar_chat_service as their entry point.
calendar_chat_model runs the provider-facing tool protocol and delegates tool calls.
operation_plan and operation_engine handle plan validation, resolution and execution.
calendar_action_gateway connects operations to the calendar business services.
"""
