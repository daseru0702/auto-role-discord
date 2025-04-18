# role_scheduler.py
import asyncio
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import aiohttp
from datetime import datetime
import config
import logging
import random

async def fetch_with_retry(session, url, headers, max_attempts=5, base_delay=1.0):
    """
    429 또는 네트워크 예외 시 지수적 백오프 + 재시도로 GET 요청을 보냅니다.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            log.info(f"[API] 시도 {attempt}/{max_attempts} GET {url}")
            async with session.get(url, headers=headers) as resp:
                # 성공
                if resp.status == 200:
                    return await resp.json()

                # rate limit
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else base_delay * (2 ** (attempt - 1))
                    delay += random.uniform(0, 0.5)
                    log.warning(f"[API] 429 Rate Limit, {delay:.1f}s 후 재시도")
                    await asyncio.sleep(delay)
                    continue

                # 그 외 에러는 재시도 없이 중단
                log.warning(f"[API] 상태 코드 {resp.status}, 중단")
                return None

        except Exception as e:
            # 네트워크 에러 등
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.error(f"[API] 예외: {e}, {delay:.1f}s 후 재시도")
            await asyncio.sleep(delay)

    log.error(f"[API] 최대 재시도({max_attempts}) 실패")
    return None

log = logging.getLogger("bot")

class RoleScheduler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler()
        trigger = CronTrigger(**config.SCHEDULE_CRON)
        self.scheduler.add_job(
            self.check_all_members,
            trigger=trigger,
            name="role_assignment_job",
            max_instances=1
        )
        bot.add_listener(self._start_scheduler, "on_ready")
        self.chunk_size = 100
        self.chunk_delay = 1.5
        self.role_semaphore = asyncio.Semaphore(3)

    async def _start_scheduler(self, *args, **kwargs):
        if not self.scheduler.running:
            self.scheduler.start()
            log.info(f"[bot] Scheduler started: {config.SCHEDULE_CRON}")

    async def check_all_members(self, ctx=None):
        if ctx:
            guild = ctx.guild
        else:
            guild = self.bot.get_guild(config.GUILD_ID)

        role = discord.utils.get(guild.roles, name=config.ROLE_EXCELLENT)
        verify_role = discord.utils.get(guild.roles, name="거래소인증")

        if not guild or not role or not verify_role:
            log.error("[ROLE_CHECK] 서버, 우수회원 역할 또는 거래소인증 역할을 찾을 수 없습니다.")
            return

        added, removed, skipped = 0, 0, 0
        added_reasons = {"join": [], "item": []}
        removed_reasons = {"join": [], "item": []}
        failed_members = []

        async with aiohttp.ClientSession() as session:
            members = guild.members
            chunks = [members[i:i+self.chunk_size] for i in range(0, len(members), self.chunk_size)]

            for chunk_index, chunk in enumerate(chunks):
                log.info(f"[ROLE_CHECK] 체크 {chunk_index+1}/{len(chunks)} 시작")
                tasks = []

                for member in chunk:
                    tasks.append(self.evaluate_member(session, guild, role, verify_role, member, failed_members, added_reasons, removed_reasons))

                results = await asyncio.gather(*tasks)
                for result in results:
                    if result == "added":
                        added += 1
                    elif result == "removed":
                        removed += 1
                    elif result == "skipped":
                        skipped += 1

                await asyncio.sleep(self.chunk_delay)

            # 2차 재시도 처리
            if failed_members:
                log.info(f"[ROLE_CHECK] 아이템레벨 재시도 대상: {len(failed_members)}명")
                retry_results = await asyncio.gather(*[
                    self.evaluate_member(session, guild, role, verify_role, member, [], added_reasons, removed_reasons, is_retry=True)
                    for member in failed_members
                ])
                for result in retry_results:
                    if result == "added":
                        added += 1
                    elif result == "removed":
                        removed += 1
                    elif result == "skipped":
                        skipped += 1

        log.info(f"[ROLE_CHECK] 완료: 부여됨 {added}, 회수됨 {removed}, 건너뜀 {skipped}")

        # 로그 파일에 사유 별 데이터 기록
        log.info(f"[DETAIL] 이용자 부여 (join): {', '.join(added_reasons['join'])}")
        log.info(f"[DETAIL] 이용자 부여 (item): {', '.join(added_reasons['item'])}")
        log.info(f"[DETAIL] 이용자 회수 (join): {', '.join(removed_reasons['join'])}")
        log.info(f"[DETAIL] 이용자 회수 (item): {', '.join(removed_reasons['item'])}")

        if ctx:
            await ctx.send(f"역할 동기화 완료\n부여: {added}명\n회수: {removed}명\n건너뜀: {skipped}명")

    async def evaluate_member(self, session, guild, role, verify_role, member, failed_members, added_reasons, removed_reasons, is_retry=False):
        async with self.role_semaphore:
            should_have_role = False                        # 조건 충족 여부
            reason = None                                   # 처리 사유
            has_role = role in member.roles                 # 우수회원 역할 보유 여부
            has_verify_role = verify_role in member.roles   # 거래소인증 역할 보유 여부
    
            # 우수회원 역할이 있지만 거래소인증 역할이 없는 경우 : 역할 회수
            if not has_verify_role:
                if has_role:
                    try:
                        await member.remove_roles(role, reason="거래소인증 없음")
                        log.info(f"[ROLE_REMOVE] {member.display_name} ← 거래소인증 없음으로 회수")
                        removed_reasons["join"].append(member.display_name)
                        return "removed"
                    except discord.Forbidden:
                        log.error(f"[ROLE_REMOVE_FAIL] 권한 부족: {member.display_name}")
                        return "skipped"
                return "skipped"
    
            # 거래소인증 역할이 있음 : 조건 검사 시작
            # 서버 가입일 검사
            joined_at = member.joined_at
            if joined_at and joined_at < config.JOIN_THRESHOLD:
                should_have_role = True
                reason = "join"
            # 서버 가입일이 기준보다 늦다면 아이템레벨 검사
            else:
                item_level = await self.fetch_max_siblings_item_level(session, member.display_name)
                if item_level is not None:
                    if item_level >= config.MIN_ITEM_LEVEL:
                        should_have_role = True
                        reason = "item"
                    else:
                        reason = "item"
                else:
                    if not is_retry:
                        failed_members.append(member)
                    return "skipped"
            
            # 역할 부여
            try:
                # 자격이 있는, 우수회원 역할 미보유자 : 역할 부여
                if should_have_role and not has_role:
                    await member.add_roles(role, reason="우수회원 자격 부여")
                    if reason:
                        added_reasons[reason].append(member.display_name)
                    return "added"
                # 자격이 없는, 우수회원 역할 보유자 : 역할 회수
                elif not should_have_role and has_role:
                    await member.remove_roles(role, reason="우수회원 조건 미달")
                    if reason:
                        removed_reasons[reason].append(member.display_name)
                    return "removed"
                else:
                    return "skipped"
            except discord.Forbidden:
                log.error(f"[ROLE_ASSIGN] {member.display_name} 역할 부여 실패: 권한 부족")
                return "skipped"
    
    async def fetch_item_level(self, session, character_name):
        url = config.LOA_API_URL.format(name=character_name)
        headers = {
            "Authorization": f"Bearer {config.LOA_API_KEY}",
            "Accept": "application/json"
        }
        # 지수적 백오프 + 재시도 호출
        data = await fetch_with_retry(session, url, headers, max_attempts=5, base_delay=1.0)
        if not data or not isinstance(data, dict):
            return None

        raw = data.get("ItemAvgLevel")
        try:
            return float(raw.replace(",", "")) if raw else None
        except Exception as e:
            log.error(f"[API] 아이템레벨 파싱 오류: {e}")
            return None


    async def fetch_max_siblings_item_level(self, session, character_name: str) -> float | None:
        url = config.LOA_SIBLINGS_URL.format(name=character_name)
        headers = {
            "Authorization": f"Bearer {config.LOA_API_KEY}",
            "Accept": "application/json"
        }
        # 지수적 백오프 + 재시도 호출
        data = await fetch_with_retry(session, url, headers, max_attempts=5, base_delay=1.0)
        if not isinstance(data, list) or len(data) == 0:
            log.warning(f"[SIBLINGS] 빈 응답 또는 검색 실패: {data} → 단일 조회로 대체")
            return await self.fetch_item_level(session, character_name)

        max_level = None
        for entry in data:
            raw = entry.get("ItemMaxLevel") or entry.get("ItemAvgLevel")
            if raw:
                lvl = float(raw.replace(",", ""))
                max_level = lvl if max_level is None else max(max_level, lvl)

        return max_level


    @commands.command(name="sync_roles")
    async def sync_roles(self, ctx):
        await self.check_all_members(ctx)
        await ctx.send("역할 동기화가 완료되었습니다.")

    @commands.command(name="check_templvl")
    async def check_templvl(self, ctx, character_name: str):
        async with aiohttp.ClientSession() as session:
            item_level = await self.fetch_item_level(session, character_name)
            if item_level:
                await ctx.send(f"{character_name}님의 아이템 레벨은 {item_level} 입니다.")
            else:
                await ctx.send(f"{character_name}님의 아이템 레벨을 가져올 수 없습니다.")

async def setup(bot):
    await bot.add_cog(RoleScheduler(bot))
