"""Authorization settings for the reserved shared demo corpus."""

from pydantic import Field
from pydantic_settings import BaseSettings


class DemoConfig(BaseSettings):
    DEMO_MAINTAINER_USER_IDS: str = Field(
        default="", description="Comma-separated user IDs authorized to maintain demos."
    )

    def get_demo_maintainer_ids(self) -> frozenset[str]:
        return frozenset(
            value.strip()
            for value in self.DEMO_MAINTAINER_USER_IDS.split(",")
            if value.strip()
        )
