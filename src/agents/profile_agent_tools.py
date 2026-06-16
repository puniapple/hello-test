"""Tool definitions for the profile-building agent."""

PROFILE_AGENT_TOOLS = [
    {
        "name": "get_current_profile",
        "description": "Получить текущее состояние профиля юзера в виде JSON-объекта. Используй в начале диалога, чтобы понять, что уже собрано, и какие поля нужно дозаполнить.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "update_profile_field",
        "description": "Обновить одно поле в профиле юзера. Используй после каждого нового факта, который юзер сообщил. Перед обновлением полей-списков (expertise, target_roles, anti_roles, interests_and_resonance и т.д.) сначала получи текущее значение через get_current_profile, и передавай в value уже полный обновлённый список (старые элементы + новые), а не только новые. Для объектов (location_preferences, compensation) — тоже передавай полный обновлённый объект.",
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": "Имя поля в профиле, например 'expertise', 'target_roles', 'compensation'.",
                    "enum": [
                        "expertise",
                        "current_role_summary",
                        "ideal_work_description",
                        "interests_and_resonance",
                        "target_roles",
                        "anti_roles",
                        "industries_interested",
                        "industries_avoid",
                        "location_preferences",
                        "format",
                        "compensation",
                        "languages",
                        "seniority",
                        "must_haves",
                        "deal_breakers",
                        "free_form_notes",
                    ],
                },
                "value": {
                    # ВАЖНО: разрешаем оба типа — массив для списков, строка для строковых полей
                    "description": (
                        "Значение поля. Массив строк для списочных полей "
                        "(target_roles, anti_roles, expertise, industries_*, must_haves, deal_breakers, languages, format). "
                        "Строка для текстовых полей. Dict для compensation и location_preferences.")
                },
            },
            "required": ["field", "value"],
        },
    },
    {
        "name": "add_cv_source",
        "description": "Добавить запись о разобранном PDF-резюме в список cv_sources профиля. Вызывай после того, как разобрал PDF и заполнил соответствующие поля профиля через update_profile_field.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Имя файла резюме",
                },
                "summary_extracted": {
                    "type": "string",
                    "description": "Краткая выжимка из резюме: годы опыта, ключевые компании, основные роли и достижения. 3-5 предложений.",
                },
            },
            "required": ["filename", "summary_extracted"],
        },
    },
    {
        "name": "finalize_editing",
        "description": "Завершить сессию редактирования профиля. Вызывай, когда юзер сказал что готов закончить, или когда ты чувствуешь, что собрал достаточно информации. В summary передай человеческую сводку профиля на русском (НЕ JSON) — что ты понял про юзера и какие вакансии будешь искать.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Краткая сводка профиля на русском. 5-10 предложений. Покажет юзеру, что ты про него понял.",
                },
            },
            "required": ["summary"],
        },
    },
]