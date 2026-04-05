# CareerSpark Backend

## Setup
1. Create a MySQL database.
2. Copy `.env.example` to `.env` and fill in values.
3. Install deps: `pip install -r requirements.txt`
4. Run: `uvicorn app.main:app --reload`

## Endpoints
- `POST /api/auth/signup`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/recommendations/generate`
- `POST /api/payments/order`
