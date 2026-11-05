"""数据模型：玩家存档的真实持久化（SQLAlchemy 2.0 + SQLite）。

设计原则：
  · 权威字段（业务会查/改的）落真实列；其余字段进 extra_json，保证与
    PLAYER_DATA dict 形状无损 round-trip（派生属性等冗余字段不丢）。
  · heroes/equips 用实例 id 主键；items 用道具 type id 主键（堆叠合并）。
  · 全部带 player_id 预留多账号（当前单角色 CHAR_ID=1001）。
"""

from datetime import datetime

from sqlalchemy import (JSON, Boolean, Float, ForeignKey, Integer, String,
                        UniqueConstraint)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship)


class Base(DeclarativeBase):
    pass


class Account(Base):
    """账号（登录鉴权的地基；本阶段只建表，鉴权流程后续接）。"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_login: Mapped[datetime | None] = mapped_column(default=None)


class Player(Base):
    """主公数据（原 player dict）。"""

    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # = CHAR_ID 1001
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), default=None)
    name: Mapped[str] = mapped_column(String(64), default="本地测试")
    lv: Mapped[int] = mapped_column(Integer, default=1)
    exp: Mapped[int] = mapped_column(Integer, default=0)
    coin: Mapped[int] = mapped_column(Integer, default=0)
    diamond: Mapped[int] = mapped_column(Integer, default=0)
    ep: Mapped[int] = mapped_column(Integer, default=0)
    vigour: Mapped[int] = mapped_column(Integer, default=0)
    vip: Mapped[int] = mapped_column(Integer, default=0)
    guide_index: Mapped[int] = mapped_column(Integer, default=0)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)

    heroes: Mapped[list["Hero"]] = relationship(
        back_populates="player", cascade="all, delete-orphan")
    equips: Mapped[list["Equip"]] = relationship(
        back_populates="player", cascade="all, delete-orphan")
    items: Mapped[list["ItemStack"]] = relationship(
        back_populates="player", cascade="all, delete-orphan")


class Hero(Base):
    """武将实例（原 heroes dict 的一项）。

    ★ 2026-10-29 账号隔离：主键从 (id) 改为 (player_id, id) —— 实例 id 是
    **玩家局部**的（每个角色都从 1 编起），全局唯一主键会让第二个角色的
    武将入库必撞。db.init() 对旧库自动重建（见 _migrate_composite_pk）。
    """

    __tablename__ = "heroes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 实例 id（玩家局部）
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id"), primary_key=True, index=True)
    type: Mapped[int] = mapped_column(Integer, index=True)      # HeroConfig.ID
    lv: Mapped[int] = mapped_column(Integer, default=1)
    exp: Mapped[int] = mapped_column(Integer, default=0)
    grade: Mapped[int] = mapped_column(Integer, default=0)
    grade_exp: Mapped[int] = mapped_column(Integer, default=0)
    awaken: Mapped[int] = mapped_column(Integer, default=0)
    star: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)
    can_break: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    avatar: Mapped[str] = mapped_column(String(128), default="")
    avatar_type: Mapped[int] = mapped_column(Integer, default=0)
    spine: Mapped[str] = mapped_column(String(128), default="")
    equips_json: Mapped[list] = mapped_column(JSON, default=list)   # 穿戴装备实例 id
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)    # 派生属性等

    player: Mapped[Player] = relationship(back_populates="heroes")


class ItemStack(Base):
    """道具堆叠（原 items dict 的一项，id = 道具 type）。

    ★ 2026-10-29 账号隔离：复合主键 (player_id, id) —— 同一道具 type 每个
    角色各有一份堆叠，全局主键必撞。
    """

    __tablename__ = "item_stacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)   # 道具 type id
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id"), primary_key=True, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)

    player: Mapped[Player] = relationship(back_populates="items")


class Equip(Base):
    """装备实例（原 equips dict 的一项）。主键 (player_id, id)，同 Hero。"""

    __tablename__ = "equips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)   # 实例 id（玩家局部）
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id"), primary_key=True, index=True)
    type: Mapped[int] = mapped_column(Integer, index=True)       # EquipConfig.ID
    host: Mapped[int] = mapped_column(Integer, default=0)        # 穿戴者武将实例 id
    strengthen: Mapped[int] = mapped_column(Integer, default=0)
    grade: Mapped[int] = mapped_column(Integer, default=0)
    grade_exp: Mapped[int] = mapped_column(Integer, default=0)
    en_crit: Mapped[int] = mapped_column(Integer, default=0)
    assist_json: Mapped[dict] = mapped_column(JSON, default=dict)
    pending_assist_json: Mapped[dict] = mapped_column(JSON, default=dict)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)

    player: Mapped[Player] = relationship(back_populates="equips")


class FateRecord(Base):
    """缘分进度（原 fate dict 的一项）。"""

    __tablename__ = "fate_records"
    __table_args__ = (UniqueConstraint("player_id", "fate_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    fate_id: Mapped[int] = mapped_column(Integer, index=True)
    groups_json: Mapped[dict] = mapped_column(JSON, default=dict)


class LevelRecord(Base):
    """关卡通关记录。

    levels 与 level_records 是两个同构 dict（EnterGame 读 levels、
    选角后 520 推送读 level_records），用 source 列区分，各自保留。
    """

    __tablename__ = "level_records"
    __table_args__ = (UniqueConstraint("player_id", "source", "level_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="levels")
    level_id: Mapped[int] = mapped_column(Integer, index=True)
    star: Mapped[int] = mapped_column(Integer, default=0)
    left_times: Mapped[int] = mapped_column(Integer, default=0)
    buy_times: Mapped[int] = mapped_column(Integer, default=0)
