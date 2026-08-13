"""
core/tools.py — LLM tool schemas for Ted.

Each tool is described in JSON Schema format for the Groq tool-calling API.
The LLM reads the user's message and picks which tool(s) to call with what args —
no hardcoded patterns, no regex, no typo sensitivity.

Adding a new capability = add an entry here + a handler in TedApi._dispatch_tool().
"""

TOOL_SCHEMAS = [
    # ── Live web ──────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live public web. Use whenever information may have changed "
                "since training (news, prices, schedules, scores, releases, current people "
                "or rules), when the user says search/look up/verify, or when fresh sources "
                "are needed. Returns dated snippets and source URLs; base the answer on them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Focused search query"}
                },
                "required": ["query"]
            }
        }
    },

    # ── Apps ──────────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": (
                "Open a macOS application. Use for any variant of 'open', 'launch', "
                "'pull up', 'start', 'bring up' + an app name. Handles typos and partial names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "App name as spoken, e.g. 'Spotify', 'Chrome', 'VS Code'"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": "Close or quit a macOS application. Use for 'close', 'quit', 'kill', 'exit' + app name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "App name to close"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browse_to",
            "description": (
                "Open a website. Use for 'go to X', 'open X website', 'browse to X', "
                "'pull up X.com'. Pass the site name or URL. If the user wants a "
                "specific browser (or is known to prefer one for this site), pass it "
                "in 'browser' — e.g. open YouTube in Brave."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {"type": "string", "description": "Website name or URL, e.g. 'amazon', 'youtube.com', 'https://google.com'"},
                    "browser": {"type": "string", "description": "Optional: specific browser to open it in, e.g. 'Brave', 'Safari', 'Chrome'"}
                },
                "required": ["site"]
            }
        }
    },

    # ── Music ─────────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": (
                "Search Spotify and play a specific song, artist, or genre. "
                "Use for 'play X', 'put on X', 'play X by Y', 'play some jazz'. "
                "Do NOT use for named playlists — use play_playlist for those."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Song title, artist name, or genre/mood"},
                    "artist": {"type": "string", "description": "Artist name if specified separately (optional)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_playlist",
            "description": "Play one of the user's saved Spotify playlists by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playlist name"},
                    "shuffle": {"type": "boolean", "description": "Whether to shuffle (default false)"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spotify_control",
            "description": "Control Spotify playback — pause, resume, skip, go back, volume. Do NOT use to play a specific song.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "next", "previous", "volume_up", "volume_down"],
                        "description": "Playback action"
                    }
                },
                "required": ["action"]
            }
        }
    },

    # ── iMessage ──────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "Send an iMessage to a contact. "
                "IMPORTANT: only set 'instruction' if the user EXPLICITLY said what the message should be about. "
                "If the user just said 'send a message to X' or 'text X' with no content, leave instruction blank — Ted will ask. "
                "Do NOT invent or assume a message. "
                "If the user mentioned a style (casual, formal, short, funny), pass it as 'style'. "
                "Example: 'text Gavin and ask if he wants to golf casually' → instruction='ask if he wants to golf', style='casual'. "
                "Example: 'send a message to Calvin' → instruction omitted. "
                "This is consequential and Ted will require user confirmation before sending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact": {"type": "string", "description": "Contact first name or full name"},
                    "instruction": {"type": "string", "description": "ONLY set if user said what to write. What the message should convey."},
                    "style": {"type": "string", "description": "Tone/style only if user specified, e.g. 'casual', 'formal', 'short and funny'"}
                },
                "required": ["contact"]
            }
        }
    },

    # ── Reminders & Timers ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Create a reminder at a specific time. Use for 'remind me to X at Y time'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What to be reminded about"},
                    "when": {"type": "string", "description": "When — e.g. 'at 3pm', 'tomorrow morning', 'Friday at 9'"}
                },
                "required": ["text", "when"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Start a countdown timer. Use for 'set a timer for X', 'timer for X minutes'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration": {"type": "string", "description": "Duration, e.g. '10 minutes', 'an hour and a half', '90 seconds'"}
                },
                "required": ["duration"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_reminders",
            "description": "List pending reminders and active timers.",
            "parameters": {"type": "object", "properties": {}}
        }
    },

    # (named-list / to-do tools removed 2026-08 — feature retired)

    # ── HUD widgets ───────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "toggle_clock",
            "description": (
                "Show or hide the clock/date/weather widget in the chat window. "
                "Use when the user asks to show, hide, or toggle the clock, the "
                "time display, or the weather widget."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["on", "off", "toggle"],
                             "description": "'on' shows it, 'off' hides it, 'toggle' flips it"}
                },
                "required": []
            }
        }
    },

    # ── Weather ───────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather conditions.",
            "parameters": {"type": "object", "properties": {}}
        }
    },

    # ── Email (Outlook) ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_emails",
            "description": (
                "Check the Outlook inbox — summarizes recent emails with sender and subject. "
                "Use for 'check my email', 'any new emails', 'what's in my inbox'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many emails to fetch (default 5, max 10)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read the content of a specific email from the inbox. Use after get_emails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "description": "Email number from the list (1 = most recent)"},
                    "mode": {
                        "type": "string",
                        "enum": ["summarized", "full"],
                        "description": "'summarized' for a short summary (default), 'full' for the whole thing"
                    }
                },
                "required": ["number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "email_action",
            "description": "Delete, flag/star, mark as read, or reply to an email. Use after get_emails. Ted requires confirmation before executing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "description": "Email number from the inbox list"},
                    "action": {
                        "type": "string",
                        "enum": ["delete", "flag", "mark_read", "reply"],
                        "description": "What to do with the email"
                    },
                    "reply_text": {"type": "string", "description": "Reply text if action is 'reply' (optional — Ted will ask for style if not provided)"}
                },
                "required": ["number", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "Compose and send a new email via Outlook. Pass what you want to say as 'instruction' "
                "— Ted generates the full email. If style is specified, include it. "
                "Ted requires confirmation before sending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "instruction": {"type": "string", "description": "What the email should say"},
                    "style": {"type": "string", "description": "Tone/style if mentioned (optional)"}
                },
                "required": ["to", "subject", "instruction"]
            }
        }
    },

    # ── Knowledge Base ────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Search Ted's personal knowledge base for information previously stored. "
                "Use for 'what do you know about X', 'do you have anything on X', "
                "'look up X in your notes'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_knowledge",
            "description": (
                "Store a piece of information in Ted's personal knowledge base. "
                "Use for 'remember this', 'add to your knowledge', 'note that', "
                "'save this for later'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The information to store"},
                    "source": {"type": "string", "description": "A short label for this knowledge, e.g. 'pricing', 'supplier info', 'product notes' (optional)"}
                },
                "required": ["text"]
            }
        }
    },

    # ── Calendar ──────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "calendar_get",
            "description": (
                "Read calendar events from Calendar.app. "
                "Use for 'what's on my calendar', 'what do I have today/tomorrow/this week', "
                "'what's my next meeting', 'do I have anything scheduled'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["today", "tomorrow", "week", "next"],
                        "description": "'today', 'tomorrow', 'week' (next 7 days), or 'next' (soonest event)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_add",
            "description": (
                "Add a new event to Calendar.app. "
                "Use for 'add a meeting', 'schedule X', 'put X on my calendar'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "when":  {"type": "string", "description": "When — e.g. 'tomorrow at 2pm', 'Friday at 9am', 'in 3 hours'"},
                    "end":   {"type": "string", "description": "End time if mentioned (optional)"},
                    "notes": {"type": "string", "description": "Event notes or description (optional)"}
                },
                "required": ["title", "when"]
            }
        }
    },

    # ── Apple Notes ───────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "notes_add",
            "description": (
                "Create or append to a note in Apple Notes. "
                "Use for 'add a note', 'make a note', 'write this down', 'append to my X note'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "body":  {"type": "string", "description": "Note content"},
                    "mode":  {
                        "type": "string",
                        "enum": ["new", "append"],
                        "description": "'new' to create a fresh note (default), 'append' to add to an existing one"
                    }
                },
                "required": ["title", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "notes_get",
            "description": (
                "Read a note from Apple Notes. "
                "Use for 'read my note about X', 'what does my X note say', 'find my note on X'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Note title or keyword to search for"}
                },
                "required": ["query"]
            }
        }
    },

    # ── Clipboard ─────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "clipboard_read",
            "description": (
                "Read the contents of the macOS clipboard. "
                "Use for 'what's in my clipboard', 'read what I copied', 'what did I copy'."
            ),
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard_write",
            "description": (
                "Write text to the macOS clipboard. "
                "Use for 'copy X to clipboard', 'put X in my clipboard'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to put in the clipboard"}
                },
                "required": ["text"]
            }
        }
    },

    # ── System controls ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "system_volume",
            "description": (
                "Control the macOS system output volume (not Spotify — use spotify_control for music). "
                "Use for 'set system volume to N', 'mute/unmute the computer', "
                "'system volume up/down'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "up", "down", "mute", "unmute", "get"],
                        "description": "Volume action"
                    },
                    "level": {
                        "type": "integer",
                        "description": "Volume level 0-100, only needed for 'set'"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "system_brightness",
            "description": (
                "Adjust the screen brightness. "
                "Use for 'increase brightness', 'dim the screen', 'turn up/down brightness'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "'up' to increase, 'down' to decrease"
                    }
                },
                "required": ["action"]
            }
        }
    },

    # ── Screen awareness ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "screen_describe",
            "description": (
                "Take a screenshot and describe what's visible on screen using vision AI. "
                "Use for 'what's on my screen', 'describe my screen', 'read this', "
                "'what does this say', 'what app is open'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What to ask about the screen, e.g. 'What text is visible?' (optional)"
                    }
                }
            }
        }
    },

    # ── Computer control ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": (
                "Type text at the current cursor position using the keyboard. "
                "Use for 'type this', 'write this for me', 'type X in the field'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"}
                },
                "required": ["text"]
            }
        }
    },

    # ── Habits ────────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "log_habit",
            "description": (
                "Log a habit completion for today. "
                "Use when the user says they did something habitual: "
                "'I worked out', 'I meditated', 'I ran', 'log that I journaled'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Habit name, e.g. 'workout', 'meditation', 'running'"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_habit_streak",
            "description": (
                "Get the current streak for a habit. "
                "Use for 'what's my workout streak', 'how many days in a row have I meditated'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Habit name to look up"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "Evaluate an arithmetic expression exactly. Use this for ANY "
                "arithmetic the user asks for — totals, percentages, splits, "
                "unit maths, tips, running costs — instead of working it out "
                "yourself. Language models are unreliable at arithmetic and "
                "this is not; the answer it returns is the correct one. "
                "Handles 'total on 3 at 45', '8 percent of 250', "
                "'what's 1200 divided by 7', 'add 15% to 89.50'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "The calculation as a plain arithmetic expression using "
                            "digits and + - * / % ** ( ). Translate the words "
                            "yourself: '8 percent of 250' becomes '0.08 * 250', "
                            "'total on 3 at 45' becomes '3 * 45', 'add 15% to 89.50' "
                            "becomes '89.50 * 1.15'. No words, no units, no currency "
                            "symbols, no equals sign."
                        ),
                    }
                },
                "required": ["expression"]
            }
        }
    },
]

# Reject invented parameter names before they can reach a Mac action. This is
# also useful documentation for local models, which tend to be more reliable
# when the schema explicitly closes the object.
for _tool in TOOL_SCHEMAS:
    _params = (_tool.get("function") or {}).get("parameters")
    if isinstance(_params, dict) and _params.get("type") == "object":
        _params.setdefault("additionalProperties", False)
