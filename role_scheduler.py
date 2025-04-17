import asyncio
import json
import time

import aiohttp
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config

class RoleScheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # 단일 ClientSession 생성
        self.session = aiohttp.ClientSession()
        # 캐시 초기화: {character_name: (level: float, timestamp: float)}
        self.cache = {}
        self.cache_ttl = 3600  # 캐시 유효기간(초)
        # 동시성 제어 세마포어 설정
        self.api_semaphore = asyncio.Semaphore(5)   # 최대 5개 API 동시 호출
        self.role_semaphore = asyncio.Semaphore(3)  # 최대 3개 역할 변경 동시 호출

        # 청크 처리 설정
        self.chunk_size = 100       # 한 청크에 처리할 멤버 수
        self.chunk_delay = 1.0      # 청크 사이 대기(초)

        # 스케줄러 생성 (즉시 시작하지 않고 on_ready에서 시작)
        self.scheduler = AsyncIOScheduler()
        trigger = CronTrigger(**config.SCHEDULE_CRON)
        self.scheduler.add_job(
            self.check_all_members,
            trigger=trigger,
            name="role_assignment_job"
        )

        # on_ready 시 스케줄러 시작
        bot.add_listener(self._start_scheduler, "on_ready")

    async def _start_scheduler(self):
        if not self.scheduler.running:
            self.scheduler.start()
            self.bot.logger.info(f"Scheduler started: {config.SCHEDULE_CRON}")

    async def fetch_item_level(self, character_name: str) -> float | None:
        """
        캐싱, 동시성 제어 및 재시도 로직이 적용된 API 호출
        """
        now = time.time()
        # 캐시 확인
        if character_name in self.cache:
            lvl, ts = self.cache[character_name]
            if now - ts < self.cache_ttl:
                self.bot.logger.info(f"[CACHE] {character_name} → {lvl}")
                return lvl

        url = config.LOA_API_URL.format(name=character_name)
        headers = {
            "Authorization": f"Bearer {config.LOA_API_KEY}",
            "Accept": "application/json"
        }
        max_retries = 3
        backoff_base = 1

        # 동시성 제어
        async with self.api_semaphore:
            for attempt in range(max_retries):
                try:
                    self.bot.logger.info(f"[API] 시도 {attempt+1} GET {url}")
                    async with self.session.get(url, headers=headers, timeout=10) as resp:
                        body = await resp.text()
                        status = resp.status
                        if status == 200:
                            data = None
                            try:
                                data = json.loads(body)
                            except json.JSONDecodeError:
                                self.bot.logger.error(f"[API] {character_name} JSON 파싱 실패: {body}")
                                return None
                            # 데이터 유효성 검사
                            if not isinstance(data, dict):
                                self.bot.logger.warning(f"[API] {character_name} 응답 데이터 없음 또는 형식 오류: {data}")
                                return None
                            lvl_str = data.get("ItemMaxLevel") or data.get("ItemAvgLevel")
                            if not lvl_str:
                                self.bot.logger.error(f"[API] {character_name} 레벨 필드 없음: {data}")
                                return None
                            try:
                                lvl = float(lvl_str.replace(",", ""))
                            except ValueError:
                                self.bot.logger.error(f"[API] {character_name} 레벨 파싱 실패: {lvl_str}")
                                return None
                            # 캐시에 저장
                            self.cache[character_name] = (lvl, now)
                            return lvl
                        elif status == 429:
                            wait = backoff_base * (2 ** attempt)
                            self.bot.logger.warning(f"[API] 레이트 리밋, {wait}s 후 재시도")
                            await asyncio.sleep(wait)
                            continue
                        else:
                            self.bot.logger.error(f"[API] {character_name} 에러 {status}: {body}")
                            return None
                except asyncio.TimeoutError:
                    self.bot.logger.warning(f"[API] {character_name} 타임아웃 시도 {attempt+1}")
                except Exception as e:
                    self.bot.logger.error(f"[API] 예외: {e}")
                # 백오프 후 재시도
                await asyncio.sleep(backoff_base * (2 ** attempt))
        return None

    async def _process_member(self, member: discord.Member, role: discord.Role) -> tuple[int, int]:
        """
        단일 멤버 처리: 역할 추가/회수 및 결과(added, removed)를 반환
        """
        added = removed = 0
        character_name = member.display_name
        joined_ok = bool(member.joined_at and member.joined_at < config.JOIN_THRESHOLD)
        lvl = await self.fetch_item_level(character_name)
        gear_ok = bool(lvl and lvl >= config.MIN_ITEM_LEVEL)
        excellent_ok = joined_ok or gear_ok

        # 역할 부여
        if excellent_ok and role not in member.roles:
            for attempt in range(3):
                try:
                    async with self.role_semaphore:
                        await member.add_roles(role)
                    added = 1
                    self.bot.logger.info(f"'{config.ROLE_EXCELLENT}' 역할 부여: {member}")
                    break
                except discord.HTTPException as e:
                    if e.status == 429:
                        wait = backoff_base * (2 ** attempt)
                        self.bot.logger.warning(f"Rate limit 부여, {wait}s 후 재시도")
                        await asyncio.sleep(wait)
                        continue
                    self.bot.logger.error(f"역할 부여 실패: {member}, {e}")
                    break

        # 역할 회수
        elif not excellent_ok and role in member.roles:
            for attempt in range(3):
                try:
                    async with self.role_semaphore:
                        await member.remove_roles(role)
                    removed = 1
                    self.bot.logger.info(f"'{config.ROLE_EXCELLENT}' 역할 회수: {member}")
                    break
                except discord.HTTPException as e:
                    if e.status == 429:
                        wait = backoff_base * (2 ** attempt)
                        self.bot.logger.warning(f"Rate limit 회수, {wait}s 후 재시도")
                        await asyncio.sleep(wait)
                        continue
                    self.bot.logger.error(f"역할 회수 실패: {member}, {e}")
                    break

        return added, removed

    async def check_all_members(self):
        """
        자동 스케줄: 전체 서버 멤버를 청크 단위로 처리
        """
        for guild in self.bot.guilds:
            role = discord.utils.get(guild.roles, name=config.ROLE_EXCELLENT)
            if not role:
                self.bot.logger.warning(f"역할 '{config.ROLE_EXCELLENT}'을 찾을 수 없습니다.")
                continue

            members = guild.members
            total = len(members)
            for start in range(0, total, self.chunk_size):
                chunk = members[start:start + self.chunk_size]
                results = await asyncio.gather(*(self._process_member(m, role) for m in chunk))
                if start + self.chunk_size < total:
                    await asyncio.sleep(self.chunk_delay)

    @commands.command(name="sync_roles")
    @commands.has_guild_permissions(administrator=True)
    async def sync_roles(self, ctx: commands.Context):
        """
        !sync_roles — 수동 동기화: 조건 만족 시 역할 부여, 아니면 회수
        """
        role = discord.utils.get(ctx.guild.roles, name=config.ROLE_EXCELLENT)
        if not role:
            return await ctx.send(f"❌ 역할 '{config.ROLE_EXCELLENT}'을 찾을 수 없습니다.")

        members = ctx.guild.members
        total = len(members)
        added = removed = 0
        async with ctx.typing():
            for start in range(0, total, self.chunk_size):
                chunk = members[start:start + self.chunk_size]
                results = await asyncio.gather(*(self._process_member(m, role) for m in chunk))
                for a, r in results:
                    added += a
                    removed += r
                if start + self.chunk_size < total:
                    await asyncio.sleep(self.chunk_delay)
        await ctx.send(f"✅ 동기화 완료: 추가 {added}건, 회수 {removed}건")

    @commands.command(name="check_templvl")
    @commands.has_guild_permissions(administrator=True)
    async def check_templvl(self, ctx: commands.Context, *, character_name: str):
        """
        !check_templvl 캐릭터명 — 테스트용: 해당 캐릭터의 평균 아이템레벨을 반환
        """
        async with ctx.typing():
            lvl = await self.fetch_item_level(character_name)

        if lvl is not None:
            await ctx.send(f"{character_name}님의 평균 아이템 레벨은 **{lvl}** 입니다.")
        else:
            await ctx.send(f"{character_name}님의 아이템 레벨을 가져오지 못했습니다.")

async def setup(bot: commands.Bot):
    await bot.add_cog(RoleScheduler(bot))
