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
                "in 'browser' ONLY when the user explicitly names it. Chrome is the "
                "default for every site except YouTube, which defaults to Brave for ad "
                "blocking. Never infer Brave for Docs or any non-YouTube site. Reuse the browser's "
                "existing window and open a new tab by default. Set new_window=true "
                "ONLY when the user explicitly asks for a new window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {"type": "string", "description": "Website name or URL, e.g. 'amazon', 'youtube.com', 'https://google.com'"},
                    "browser": {"type": "string", "description": "Optional: specific browser to open it in, e.g. 'Brave', 'Safari', 'Chrome'"},
                    "new_window": {"type": "boolean", "description": "Default false. True only if the user explicitly said to open a new window."}
                },
                "required": ["site"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_youtube",
            "description": (
                "Search YouTube, open the first concrete video result, and start playback. "
                "Use for the complete outcome whenever the user asks to play or watch a "
                "YouTube video; do not stop after browse_to opens the home page. An empty "
                "query means any popular video. YouTube defaults to Brave."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Video topic/title; empty for any popular video"},
                    "browser": {"type": "string", "description": "Optional browser named by the user"}
                }
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
            "description": (
                "Control or inspect verified Spotify playback — pause, resume, skip, "
                "go back, volume, or report the current track. Use action=current for "
                "questions about what is playing. Do NOT use to play a specific song."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "next", "previous", "volume_up", "volume_down", "current"],
                        "description": "Playback action"
                    }
                },
                "required": ["action"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "add_to_playlist",
            "description": (
                "Add a track to one of the user's Spotify playlists. Leave "
                "'track' empty to add whatever is playing right now — that is the "
                "usual case ('add this to my gym playlist')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "playlist": {"type": "string", "description": "Playlist name"},
                    "track": {"type": "string", "description": "Song to add. Omit to use the currently playing track."}
                },
                "required": ["playlist"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_playlist",
            "description": (
                "Remove a track from one of the user's Spotify playlists. Leave "
                "'track' empty to remove whatever is playing right now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "playlist": {"type": "string", "description": "Playlist name"},
                    "track": {"type": "string", "description": "Song to remove. Omit to use the currently playing track."}
                },
                "required": ["playlist"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_playlist",
            "description": "Create a new Spotify playlist on the user's account. Private unless asked otherwise.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new playlist"},
                    "public": {"type": "boolean", "description": "Make it public (default false)"},
                    "description": {"type": "string", "description": "Optional playlist description"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_playlist",
            "description": (
                "Remove a playlist from the user's Spotify library. Spotify has NO "
                "delete endpoint — this UNFOLLOWS the playlist, which is exactly what "
                "the Spotify app's own 'Delete playlist' does. Report it as removed "
                "from their library, never as permanently deleted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playlist name"}
                },
                "required": ["name"]
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
                "TEXT vs INSTRUCTION decides whether the user's own words get sent or "
                "rewritten, so get this right. "
                "(1) The user gave the actual words, usually in quotes: put them in "
                "'text' EXACTLY as written. Keep their spelling, slang, casing and "
                "missing punctuation. Do not tidy, rephrase, or add a greeting. "
                "Example: send gavin \"otw be there in 10\" → text='otw be there in 10'. "
                "(2) The user said what to convey but not the words: use 'instruction' "
                "and Ted writes it. Example: 'text Gavin that I'll be late to golf' → "
                "instruction='tell him I will be late to golf'. "
                "(3) The user gave neither: set only 'contact' and Ted will ask. Never "
                "invent a message. "
                "Never set both 'text' and 'instruction'. 'style' applies only to "
                "'instruction' — words the user wrote themselves are never restyled. "
                "This is consequential and Ted will require user confirmation before sending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact": {"type": "string", "description": "Contact first name or full name"},
                    "text": {"type": "string", "description": "The user's OWN words, sent verbatim and never edited. Use this whenever they supplied the actual message."},
                    "instruction": {"type": "string", "description": "ONLY when the user said what to convey but not the exact words. Ted writes the message from this."},
                    "style": {"type": "string", "description": "Tone/style, only with 'instruction'. Words the user wrote are never restyled."}
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
                "Last-resort visual inspection: take a temporary screenshot and describe "
                "what semantic accessibility cannot expose. Prefer ui_inspect for apps and "
                "well-structured web pages. The image is stored only in the macOS temporary "
                "folder and deleted immediately after vision reads it; no history is kept."
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
    {
        "type": "function",
        "function": {
            "name": "ui_inspect",
            "description": (
                "Inspect the frontmost app's accessibility tree without a screenshot. In "
                "Brave, Chrome, Safari, and other browsers this exposes documented HTML "
                "buttons, links, form fields, headings, and video controls. Always use this "
                "before visual screen inspection on a structured page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional label fragment to filter for"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ui_press",
            "description": (
                "Press a visible named button, link, menu item, or video control in "
                "the frontmost app. Uses macOS Accessibility first and high-confidence "
                "screen vision only when the app exposes no matching control. Never use "
                "for destructive or purchase confirmation controls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Visible control label, e.g. Blank document, Play, or Share"}
                },
                "required": ["target"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ui_fill",
            "description": (
                "Fill a labeled native or HTML input field through macOS Accessibility, "
                "without taking a screenshot. Use for search boxes, text inputs, and forms "
                "whose label or placeholder is visible in ui_inspect."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Field label, placeholder, or accessible name"},
                    "text": {"type": "string", "description": "Text to put in the field"}
                },
                "required": ["target", "text"]
            }
        }
    },

    # ── Computer control ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": (
                "Draft content, create a fresh Google Doc or TextEdit document, type it, "
                "and apply requested formatting as one complete workflow. Pass a compact "
                "description of what to write in instructions; DO NOT generate the full "
                "document inside this tool call. Google Docs uses Chrome by default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instructions": {"type": "string", "description": "Compact writing request, topic, audience, and requirements; not the drafted document"},
                    "target_words": {"type": "integer", "description": "Approximate word count; infer about 300 words per double-spaced page"},
                    "font_size": {"type": "integer", "description": "Requested point size, usually 10-18"},
                    "line_spacing": {"type": "string", "enum": ["single", "1.15", "1.5", "double"], "description": "Requested line spacing"},
                    "app": {"type": "string", "enum": ["google_docs", "textedit"], "description": "Default google_docs"},
                    "browser": {"type": "string", "description": "Browser for Google Docs; default Google Chrome. Set only when the user explicitly names another browser."}
                },
                "required": ["instructions"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "learn_lingo",
            "description": (
                "Remember an explicit explanation of Charlie's personal shorthand. Use "
                "only when Charlie directly says what one of his terms means."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "Charlie's shorthand"},
                    "meaning": {"type": "string", "description": "Canonical meaning"}
                },
                "required": ["term", "meaning"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clarify_lingo",
            "description": (
                "Ask Charlie what an unfamiliar personal term means and arm the next "
                "reply to save his explanation. Use only when that term blocks the task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "The exact unfamiliar term"}
                },
                "required": ["term"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": (
                "Type text at the current cursor position using the keyboard. "
                "Use for an already-focused editor. For a new document, use "
                "create_document; for a labeled HTML field, use ui_fill."
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
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a keyboard key or shortcut in the frontmost app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": ["enter", "tab", "escape", "space", "delete", "backspace", "left", "right", "up", "down", "copy", "paste", "cut", "undo", "redo", "select all", "save"]}
                },
                "required": ["key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the frontmost app up or down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "amount": {"type": "integer", "description": "Approximate pixels, 80-2400; defaults to 600"}
                },
                "required": ["direction"]
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
