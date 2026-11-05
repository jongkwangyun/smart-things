package com.smartthings.cleaner

import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import java.util.Locale

/**
 * 달력+시간 휠 공용 피커(DateTimeWheelPicker)의 순수 로직. Android 의존성 없음(JVM 단위 테스트 대상).
 * 모든 일시는 KST(Asia/Seoul) 기준이다.
 */
object DateTimePickerLogic {
    val KST: ZoneId = ZoneId.of("Asia/Seoul")

    private val SUMMARY_FORMATTER: DateTimeFormatter =
        DateTimeFormatter.ofPattern("yyyy-MM-dd (E) a h:mm", Locale.KOREAN)

    /** 현재 KST 시각(분 단위 절삭). */
    fun nowKst(): ZonedDateTime = ZonedDateTime.now(KST).truncatedTo(ChronoUnit.MINUTES)

    /** 임의의 ZonedDateTime 을 KST 분 단위로 정규화한다. */
    fun toKst(value: ZonedDateTime): ZonedDateTime =
        value.withZoneSameInstant(KST).truncatedTo(ChronoUnit.MINUTES)

    /** 오전/오후 + 12시간제 시(1~12) -> 24시간제 시(0~23). 오전 12시=0, 오후 12시=12. */
    fun to24Hour(isPm: Boolean, hour12: Int): Int {
        require(hour12 in 1..12) { "hour12 must be 1..12: $hour12" }
        return (hour12 % 12) + if (isPm) 12 else 0
    }

    /** 24시간제 시(0~23) -> (오후 여부, 12시간제 시 1~12). */
    fun from24Hour(hour24: Int): Pair<Boolean, Int> {
        require(hour24 in 0..23) { "hour24 must be 0..23: $hour24" }
        val h12 = if (hour24 % 12 == 0) 12 else hour24 % 12
        return Pair(hour24 >= 12, h12)
    }

    /** 달력 날짜 + 휠 값으로 KST 일시를 만든다. */
    fun compose(date: LocalDate, isPm: Boolean, hour12: Int, minute: Int): ZonedDateTime =
        ZonedDateTime.of(date, java.time.LocalTime.of(to24Hour(isPm, hour12), minute), KST)

    /** value 가 minDateTime 보다 과거이면 true (분 단위 비교). */
    fun isPast(value: ZonedDateTime, minDateTime: ZonedDateTime): Boolean =
        toKst(value).isBefore(toKst(minDateTime))

    /** 과거이면 minDateTime 으로 보정한다. */
    fun clampToMin(value: ZonedDateTime, minDateTime: ZonedDateTime): ZonedDateTime =
        if (isPast(value, minDateTime)) toKst(minDateTime) else toKst(value)

    /** 하단 요약 문구. 예: "2026-11-05 (목) 오후 3:07 KST" */
    fun summary(value: ZonedDateTime): String = toKst(value).format(SUMMARY_FORMATTER) + " KST"
}
