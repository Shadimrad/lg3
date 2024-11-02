from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, date
from typing import List, Dict, Optional
import databases
import sqlalchemy
from sqlalchemy import create_engine

# Database setup
DATABASE_URL = "sqlite:///./habits.db"
database = databases.Database(DATABASE_URL)
metadata = sqlalchemy.MetaData()

# Database tables
sprints = sqlalchemy.Table(
    "sprints",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("name", sqlalchemy.String),
    sqlalchemy.Column("start_date", sqlalchemy.Date),
    sqlalchemy.Column("end_date", sqlalchemy.Date),
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, default=datetime.utcnow),
)

habits = sqlalchemy.Table(
    "habits",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("sprint_id", sqlalchemy.Integer, sqlalchemy.ForeignKey("sprints.id")),
    sqlalchemy.Column("name", sqlalchemy.String),
    sqlalchemy.Column("weight", sqlalchemy.Float),  # Percentage (0-100)
    sqlalchemy.Column("target_hours", sqlalchemy.Float),
)

effort_logs = sqlalchemy.Table(
    "effort_logs",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("habit_id", sqlalchemy.Integer, sqlalchemy.ForeignKey("habits.id")),
    sqlalchemy.Column("date", sqlalchemy.Date),
    sqlalchemy.Column("hours", sqlalchemy.Float),
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, default=datetime.utcnow),
)

# Pydantic models for request/response
class HabitCreate(BaseModel):
    name: str
    weight: float
    target_hours: float

class SprintCreate(BaseModel):
    name: str
    start_date: date
    end_date: date
    habits: List[HabitCreate]

class EffortCreate(BaseModel):
    habit_id: int
    date: date
    hours: float

class SprintResponse(BaseModel):
    id: int
    name: str
    start_date: date
    end_date: date
    habits: List[dict]
    days: List[Optional[float]]

# Create database engine
engine = create_engine(DATABASE_URL)
metadata.create_all(engine)

# Initialize FastAPI app
app = FastAPI(title="Habit Tracker API")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# Sprint endpoints
@app.post("/api/sprints", response_model=dict)
async def create_sprint(sprint: SprintCreate):
    try:
        async with database.transaction():
            # Create sprint
            sprint_query = sprints.insert().values(
                name=sprint.name,
                start_date=sprint.start_date,
                end_date=sprint.end_date
            )
            sprint_id = await database.execute(sprint_query)
            
            # Create habits and store their IDs
            habit_ids = {}
            for habit in sprint.habits:
                habit_query = habits.insert().values(
                    sprint_id=sprint_id,
                    name=habit.name,
                    weight=habit.weight,
                    target_hours=habit.target_hours
                )
                habit_id = await database.execute(habit_query)
                habit_ids[habit.name] = habit_id
            
            return {"sprint_id": sprint_id, "habit_ids": habit_ids}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sprints/{sprint_id}")
async def get_sprint(sprint_id: int):
    try:
        # Get sprint details
        sprint_query = sprints.select().where(sprints.c.id == sprint_id)
        sprint = await database.fetch_one(sprint_query)
        
        if not sprint:
            raise HTTPException(status_code=404, detail="Sprint not found")
        
        # Get habits for the sprint
        habit_query = habits.select().where(habits.c.sprint_id == sprint_id)
        sprint_habits = await database.fetch_all(habit_query)
        
        # Calculate days array
        days_count = (sprint.end_date - sprint.start_date).days + 1
        days = [None] * days_count
        
        # Get all effort logs for the sprint
        effort_query = """
        SELECT e.date, e.hours, e.habit_id, h.weight, h.target_hours
        FROM effort_logs e
        JOIN habits h ON e.habit_id = h.id
        WHERE h.sprint_id = :sprint_id
        """
        efforts = await database.fetch_all(query=effort_query, values={"sprint_id": sprint_id})
        
        # Calculate daily scores
        for effort in efforts:
            day_index = (effort.date - sprint.start_date).days
            if 0 <= day_index < days_count:
                progress = min(effort.hours / effort.target_hours, 1)
                score = progress * effort.weight
                if days[day_index] is None:
                    days[day_index] = score
                else:
                    days[day_index] += score
        
        return {
            "id": sprint.id,
            "name": sprint.name,
            "start_date": sprint.start_date,
            "end_date": sprint.end_date,
            "habits": [dict(h) for h in sprint_habits],
            "days": days
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sprints/{sprint_id}/efforts/{date}")
async def get_daily_efforts(sprint_id: int, date: date):
    try:
        # Get all habits for the sprint
        habit_query = habits.select().where(habits.c.sprint_id == sprint_id)
        sprint_habits = await database.fetch_all(habit_query)
        
        if not sprint_habits:
            raise HTTPException(status_code=404, detail="Sprint not found or has no habits")
        
        # Get all efforts for these habits on the given date
        efforts = []
        for habit in sprint_habits:
            effort_query = effort_logs.select().where(
                (effort_logs.c.habit_id == habit.id) &
                (effort_logs.c.date == date)
            )
            effort = await database.fetch_one(effort_query)
            
            efforts.append({
                "habit_name": habit.name,
                "habit_id": habit.id,
                "hours": effort.hours if effort else 0
            })
        
        return {"efforts": efforts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/efforts")
async def log_effort(effort: EffortCreate):
    try:
        # Check if an effort already exists for this habit and date
        existing_query = effort_logs.select().where(
            (effort_logs.c.habit_id == effort.habit_id) &
            (effort_logs.c.date == effort.date)
        )
        existing_effort = await database.fetch_one(existing_query)
        
        if existing_effort:
            # Update existing effort
            query = effort_logs.update().where(
                (effort_logs.c.habit_id == effort.habit_id) &
                (effort_logs.c.date == effort.date)
            ).values(hours=effort.hours)
            await database.execute(query)
            return {"message": "Effort updated successfully"}
        else:
            # Create new effort
            query = effort_logs.insert().values(
                habit_id=effort.habit_id,
                date=effort.date,
                hours=effort.hours
            )
            effort_id = await database.execute(query)
            return {"effort_id": effort_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/sprints/{sprint_id}")
async def delete_sprint(sprint_id: int):
    try:
        async with database.transaction():
            # Delete all effort logs for habits in this sprint
            habit_query = habits.select().where(habits.c.sprint_id == sprint_id)
            sprint_habits = await database.fetch_all(habit_query)
            
            for habit in sprint_habits:
                await database.execute(
                    effort_logs.delete().where(effort_logs.c.habit_id == habit.id)
                )
            
            # Delete all habits for this sprint
            await database.execute(
                habits.delete().where(habits.c.sprint_id == sprint_id)
            )
            
            # Delete the sprint
            await database.execute(
                sprints.delete().where(sprints.c.id == sprint_id)
            )
            
            return {"message": "Sprint deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))