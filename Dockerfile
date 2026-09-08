# Build stage: needs the devDependencies (TypeScript) to produce dist/.
FROM node:24-alpine AS build

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# Runtime stage: production dependencies and the compiled output only.
FROM node:24-alpine

WORKDIR /app

ENV NODE_ENV=production

COPY package*.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=build /app/dist ./dist

USER node

# "start" keeps the process alive and pings on SUPAWAKE_INTERVAL.
# Projects come from SUPABASE_<n>_URL / _KEY / _TABLE in the environment.
CMD ["node", "dist/index.js", "start"]
