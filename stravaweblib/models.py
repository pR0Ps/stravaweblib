from typing import List

from pydantic import BaseModel, Field


class Athlete(BaseModel):
    avatar_url: str
    firstname: str
    id: int
    is_following: bool
    is_private: bool
    location: str
    member_type: str
    name: str
    url: str


class Kudos(BaseModel):
    athletes: List[Athlete] = Field(default_factory=list)
    is_owner: bool
    kudosable: bool


class MentionableAthlete(BaseModel):
    display: str
    id: str
    location: str
    member_type: str
    profile: str
    type: str


class MentionableClub(BaseModel):
    display: str
    id: str
    image: str
    location: str
    type: str
