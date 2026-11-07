from __future__ import annotations

from collections.abc import Generator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from packages.common.database import service_lifespan, session_dependency
from packages.common.events import emit_catalog_snapshot
from packages.seed_data import COUNTRIES, CITIES, attraction_rows


class DestinationBase(DeclarativeBase):
    pass


class Country(DestinationBase):
    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class City(DestinationBase):
    __tablename__ = "cities"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    country_code: Mapped[str] = mapped_column(ForeignKey("countries.code"), index=True)


class Attraction(DestinationBase):
    __tablename__ = "attractions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    city_code: Mapped[str] = mapped_column(ForeignKey("cities.code"), index=True)


class CountryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str


class CityView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    country_code: str


class AttractionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    city_code: str


def _seed(session: Session) -> None:
    if session.scalar(select(Country.code).limit(1)) is not None:
        return
    session.add_all(Country(code=code, name=name) for code, name in COUNTRIES)
    session.flush()
    session.add_all(
        City(code=code, name=name, country_code=country)
        for code, name, country in CITIES
    )
    session.flush()
    session.add_all(
        Attraction(id=attraction_id, name=name, city_code=city_code)
        for attraction_id, name, city_code in attraction_rows()
    )
    session.commit()


def _catalog_snapshot(session: Session) -> None:
    documents = [
        {
            "id": city.code,
            "kind": "destination",
            "name": city.name,
            "city_code": city.code,
            "country_code": city.country_code,
        }
        for city in session.scalars(select(City).order_by(City.code))
    ]
    documents.extend(
        {
            "id": attraction.id,
            "kind": "attraction",
            "name": attraction.name,
            "city_code": attraction.city_code,
        }
        for attraction in session.scalars(select(Attraction).order_by(Attraction.id))
    )
    emit_catalog_snapshot(session, producer="destination", documents=documents)


def _session(request: Request) -> Generator[Session, None, None]:
    yield from session_dependency(request)


app = FastAPI(
    title="Destination Service",
    version="0.1.0",
    lifespan=service_lifespan(
        base=DestinationBase,
        seed=_seed,
        default_database_url="sqlite:///./destination.db",
        events_producer="destination",
        bootstrap_events=_catalog_snapshot,
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "destination", "status": "ok"}


@app.get("/countries", response_model=list[CountryView])
def list_countries(session: Session = Depends(_session)) -> list[Country]:
    return list(session.scalars(select(Country).order_by(Country.name)))


@app.get("/cities", response_model=list[CityView])
def list_cities(
    country_code: str | None = None,
    query: str | None = None,
    session: Session = Depends(_session),
) -> list[City]:
    statement = select(City)
    if country_code:
        statement = statement.where(City.country_code == country_code.upper())
    if query:
        statement = statement.where(City.name.ilike(f"%{query.strip()}%"))
    return list(session.scalars(statement.order_by(City.name)))


@app.get("/destinations/{city_code}", response_model=CityView)
def get_destination(
    city_code: str,
    session: Session = Depends(_session),
) -> City:
    city = session.get(City, city_code.upper())
    if city is None:
        raise HTTPException(status_code=404, detail="Destination not found")
    return city


@app.get("/destinations/{city_code}/attractions", response_model=list[AttractionView])
def list_attractions(
    city_code: str,
    query: str | None = None,
    session: Session = Depends(_session),
) -> list[Attraction]:
    statement = select(Attraction).where(Attraction.city_code == city_code.upper())
    if query:
        statement = statement.where(Attraction.name.ilike(f"%{query.strip()}%"))
    results = list(session.scalars(statement.order_by(Attraction.name)))
    if session.get(City, city_code.upper()) is None:
        raise HTTPException(status_code=404, detail="Destination not found")
    return results


@app.get("/attractions", response_model=list[AttractionView])
def search_attractions(
    city_code: str | None = None,
    query: str | None = None,
    session: Session = Depends(_session),
) -> list[Attraction]:
    statement = select(Attraction)
    if city_code:
        statement = statement.where(Attraction.city_code == city_code.upper())
    if query:
        statement = statement.where(Attraction.name.ilike(f"%{query.strip()}%"))
    return list(session.scalars(statement.order_by(Attraction.city_code, Attraction.name)))
