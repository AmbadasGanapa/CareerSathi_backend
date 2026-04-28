# CareerSpark Backend

## Local setup
1. Copy `.env.example` to `.env`.
2. Set `MONGODB_URL` to your MongoDB Atlas connection string.
3. Set `FRONTEND_ORIGIN` to your frontend URL.
4. Install dependencies with `pip install -r requirements.txt`.
5. Run the API with `uvicorn app.main:app --reload`.

## Render deployment
1. Create a Render web service for the `backend` folder.
2. Set the start command to `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
3. Add the environment variables from `.env.example`.
4. Use your MongoDB Atlas URI in `MONGODB_URL`.
5. Set `FRONTEND_ORIGIN` and `FRONTEND_ORIGINS` to your Vercel domain and any custom domain.

## Notes
- The API is served under the `/api` prefix.
- Health checks are available at `/health` and `/ping`.
- MongoDB Atlas is the active database path. There is no Railway/MySQL dependency in the runtime flow.
