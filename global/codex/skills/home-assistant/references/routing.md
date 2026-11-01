# Routing

Explicit owner home requests only. Examples: "turn off the office
light", "is the kitchen light on". Small talk, automation echoes, and
scheduled Office messages are NOT owner requests.

Order: native ha_* tools first, fallback client second. No extra model
call for a routine action: one routing decision, one tool call, one
verified readback.

Native tool map:

- list/find -> ha_list_entities (filter domain light client-side)
- state -> ha_get_state
- services -> ha_list_services
- write -> ha_call_service with light.turn_on / light.turn_off only

Fallback map (scripts/ha_client.py): health, state, find, services,
lights-on, lights-off. Same allowlists and dry-run rule.
