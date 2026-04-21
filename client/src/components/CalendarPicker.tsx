import { useState } from "react";
import type { DateFilter } from "../lib/api";

type Mode = "single" | "range" | "multi";

type Props = {
  filters: DateFilter[];
  onChange: (filters: DateFilter[]) => void;
};

const weekdays = ["一", "二", "三", "四", "五", "六", "日"];

function toIso(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function sameMonth(left: Date, right: Date): boolean {
  return left.getFullYear() === right.getFullYear() && left.getMonth() === right.getMonth();
}

function daysForMonth(anchor: Date): Date[] {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const mondayOffset = (first.getDay() + 6) % 7;
  const start = new Date(first);
  start.setDate(first.getDate() - mondayOffset);
  return Array.from({ length: 42 }, (_, index) => {
    const day = new Date(start);
    day.setDate(start.getDate() + index);
    return day;
  });
}

function isInsideFilter(dayIso: string, filter: DateFilter): boolean {
  const end = filter.end || filter.start;
  return dayIso >= filter.start && dayIso <= end;
}

function summarize(filters: DateFilter[]): string {
  if (filters.length === 0) return "不限制日期，抓取最新";
  if (filters.length === 1) {
    const item = filters[0];
    return item.end && item.end !== item.start ? `${item.start} 至 ${item.end}` : item.start;
  }
  return `${filters.length} 个离散日期`;
}

export function CalendarPicker({ filters, onChange }: Props) {
  const today = new Date();
  const [visibleMonth, setVisibleMonth] = useState(today);
  const [mode, setMode] = useState<Mode>("single");
  const [rangeStart, setRangeStart] = useState("");

  const days = daysForMonth(visibleMonth);

  function moveMonth(delta: number) {
    setVisibleMonth(new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + delta, 1));
  }

  function pickDay(day: Date) {
    const iso = toIso(day);
    if (mode === "single") {
      onChange([{ start: iso }]);
      return;
    }
    if (mode === "multi") {
      const exists = filters.some((item) => !item.end && item.start === iso);
      onChange(exists ? filters.filter((item) => item.start !== iso) : [...filters, { start: iso }].sort((a, b) => a.start.localeCompare(b.start)));
      return;
    }
    if (!rangeStart) {
      setRangeStart(iso);
      onChange([{ start: iso }]);
      return;
    }
    const start = iso < rangeStart ? iso : rangeStart;
    const end = iso < rangeStart ? rangeStart : iso;
    onChange([{ start, end }]);
    setRangeStart("");
  }

  function switchMode(nextMode: Mode) {
    setMode(nextMode);
    setRangeStart("");
    if (nextMode !== "multi" && filters.length > 1) {
      onChange(filters.slice(0, 1));
    }
  }

  return (
    <section className="calendar-box">
      <div className="calendar-topline">
        <p className="section-label">抓取日期</p>
        <button className="text-button" onClick={() => onChange([])}>清空</button>
      </div>
      <div className="mode-tabs compact calendar-modes">
        <button className={mode === "single" ? "active" : ""} onClick={() => switchMode("single")}>单天</button>
        <button className={mode === "range" ? "active" : ""} onClick={() => switchMode("range")}>范围</button>
        <button className={mode === "multi" ? "active" : ""} onClick={() => switchMode("multi")}>多天</button>
      </div>
      <div className="calendar-nav">
        <button onClick={() => moveMonth(-1)} aria-label="上个月">‹</button>
        <strong>{visibleMonth.getFullYear()} 年 {visibleMonth.getMonth() + 1} 月</strong>
        <button onClick={() => moveMonth(1)} aria-label="下个月">›</button>
      </div>
      <div className="calendar-grid weekdays">
        {weekdays.map((day) => <span key={day}>{day}</span>)}
      </div>
      <div className="calendar-grid">
        {days.map((day) => {
          const iso = toIso(day);
          const selected = filters.some((filter) => isInsideFilter(iso, filter));
          const pending = rangeStart === iso;
          return (
            <button
              key={iso}
              className={[
                "calendar-day",
                sameMonth(day, visibleMonth) ? "" : "muted-day",
                selected ? "selected" : "",
                pending ? "pending" : ""
              ].join(" ")}
              onClick={() => pickDay(day)}
            >
              {day.getDate()}
            </button>
          );
        })}
      </div>
      <p className="calendar-summary">{summarize(filters)}</p>
    </section>
  );
}
