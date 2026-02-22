from typing import Dict

class PersonaService:
    # Mock data
    MOCK_USERS = {
        "user_1": "NEW",
        "user_2": "RETURNING",
        "user_3": "POWER",
    }

    def get_persona(self, user_id: str) -> str:
        return self.MOCK_USERS.get(user_id, "NEW")
