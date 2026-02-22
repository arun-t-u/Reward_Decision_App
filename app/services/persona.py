from typing import Dict

class PersonaService:
    """
    Persona service to determine the persona of a user.
    """
    # Mock data
    MOCK_USERS = {
        "user_1": "NEW",
        "user_2": "RETURNING",
        "user_3": "POWER",
    }

    def get_persona(self, user_id: str) -> str:
        """
        Get the persona for the given user.
        """
        return self.MOCK_USERS.get(user_id, "NEW")
