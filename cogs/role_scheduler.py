import asyncio
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import aiohttp
from datetime import datetime
import config
import logging

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
        failed_members = []

        async with aiohttp.ClientSession() as session:
            members = guild.members
            chunks = [members[i:i+self.chunk_size] for i in range(0, len(members), self.chunk_size)]

            for chunk_index, chunk in enumerate(chunks):
                log.info(f"[ROLE_CHECK] 청크 {chunk_index+1}/{len(chunks)} 시작")
                tasks = []

                for member in chunk:
                    tasks.append(self.evaluate_member(session, guild, role, verify_role, member, failed_members))

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
                log.info(f"[ROLE_CHECK] 템렙 재시도 대상: {len(failed_members)}명")
                retry_results = await asyncio.gather(*[
                    self.evaluate_member(session, guild, role, verify_role, member, [], is_retry=True)
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

        if ctx:
            await ctx.send(f"역할 동기화 완료\n부여: {added}명\n회수: {removed}명\n건너뜀: {skipped}명")

    async def evaluate_member(self, session, guild, role, verify_role, member, failed_members, is_retry=False):
        async with self.role_semaphore:
            should_have_role = False
            has_role = role in member.roles
            has_verify_role = verify_role in member.roles

            if not has_verify_role:
                log.debug(f"[ROLE_CHECK] {member.display_name} - 거래소인증 역할 없음")
                return "skipped"

            joined_at = member.joined_at
            if joined_at and joined_at < config.JOIN_THRESHOLD:
                should_have_role = True
                log.debug(f"[ROLE_CHECK] {member.display_name} - 가입일 조건 충족")
            else:
                item_level = await self.fetch_item_level(session, member.display_name)
                if item_level is not None:
                    if item_level >= config.MIN_ITEM_LEVEL:
                        should_have_role = True
                        log.debug(f"[ROLE_CHECK] {member.display_name} - 템렙 조건 충족 ({item_level})")
                    else:
                        log.debug(f"[ROLE_CHECK] {member.display_name} - 템렙 낮음 ({item_level})")
                else:
                    log.warning(f"[ROLE_CHECK] {member.display_name} - 템렙 정보 가져오기 실패")
                    if not is_retry:
                        failed_members.append(member)
                    return "skipped"

            try:
                if should_have_role and not has_role:
                    await member.add_roles(role, reason="우수회원 자격 부여")
                    log.info(f"[ROLE_ASSIGN] {member.display_name} → 역할 부여")
                    return "added"
                elif not should_have_role and has_role:
                    await member.remove_roles(role, reason="우수회원 조건 미충족")
                    log.info(f"[ROLE_REMOVE] {member.display_name} → 역할 회수")
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

        for attempt in range(3):
            try:
                log.info(f"[API] 시도 {attempt+1} GET {url}")
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            if data and isinstance(data, dict):
                                raw_level = data.get("ItemAvgLevel")
                                return float(raw_level.replace(",", "")) if raw_level else None
                            else:
                                log.warning(f"[API] {character_name} 응답이 비어있거나 잘못된 구조입니다.")
                                return None
                        except Exception as parse_err:
                            log.error(f"[API] JSON 파싱 오류: {parse_err}")
                            return None
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "3"))
                        log.warning(f"[API] {character_name} → 429 Rate Limit. {retry_after}초 후 재시도")
                        await asyncio.sleep(retry_after)
                    else:
                        log.warning(f"[API] {character_name} → {resp.status}")
            except Exception as e:
                log.error(f"[API] 예외: {e}")
                await asyncio.sleep(1 + attempt * 2)
        return None

    @commands.command(name="sync_roles")
    async def sync_roles(self, ctx):
        await self.check_all_members(ctx)
        await ctx.send("역할 동기화가 완료되었습니다.")

    @commands.command(name="check_templvl")
    async def check_templvl(self, ctx, character_name: str):
        async with aiohttp.ClientSession() as session:
            item_level = await self.fetch_item_level(session, character_name)
            if item_level:
                await ctx.send(f"{character_name}님의 평균 아이템 레벨은 {item_level} 입니다.")
            else:
                await ctx.send(f"{character_name}님의 아이템 레벨을 가져올 수 없습니다.")


async def setup(bot):
    await bot.add_cog(RoleScheduler(bot))
