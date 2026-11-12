package com.smartthings.cleaner

import com.smartthings.cleaner.model.CreateReservationResult
import com.smartthings.cleaner.model.ReservationItem
import com.smartthings.cleaner.model.UpdateReservationResult
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 청소 방식(먼지청소·물청소·먼지+물청소) 라벨 변환, 모델 매핑, 기본값 단위 테스트 (REQ-001, REQ-002, REQ-003, LB-058).
 */
class CleaningTypeLogicTest {

    @Test
    fun testCleaningTypeToLabelMapping() {
        assertEquals("먼지청소", ReservationItem.cleaningTypeToLabel("vacuum"))
        assertEquals("물청소", ReservationItem.cleaningTypeToLabel("mop"))
        assertEquals("먼지 + 물청소", ReservationItem.cleaningTypeToLabel("mopAfterVacuum"))
        assertEquals("먼지 + 물청소", ReservationItem.cleaningTypeToLabel("vacuumAndMopTogether"))
        // 미지정/알 수 없는 값은 기본 "먼지 + 물청소"
        assertEquals("먼지 + 물청소", ReservationItem.cleaningTypeToLabel(null))
        assertEquals("먼지 + 물청소", ReservationItem.cleaningTypeToLabel("unknownType"))
    }

    @Test
    fun testLabelToCleaningTypeMapping() {
        assertEquals("vacuum", ReservationItem.labelToCleaningType("먼지청소"))
        assertEquals("mop", ReservationItem.labelToCleaningType("물청소"))
        assertEquals("mopAfterVacuum", ReservationItem.labelToCleaningType("먼지 + 물청소"))
        // 미지원 라벨은 기본값
        assertEquals("mopAfterVacuum", ReservationItem.labelToCleaningType("알 수 없음"))
    }

    @Test
    fun testReservationItemWithCleaningType() {
        val itemVacuum = ReservationItem(
            id = "res-1",
            scheduledAt = "2026-11-15T10:00:00+09:00",
            status = "예정",
            cleaningType = "vacuum"
        )
        assertEquals("vacuum", itemVacuum.cleaningType)
        assertEquals("먼지청소", itemVacuum.getCleaningTypeLabel())

        val itemMop = ReservationItem(
            id = "res-2",
            scheduledAt = "2026-11-15T11:00:00+09:00",
            status = "예정",
            cleaningType = "mop"
        )
        assertEquals("mop", itemMop.cleaningType)
        assertEquals("물청소", itemMop.getCleaningTypeLabel())

        val itemTogether = ReservationItem(
            id = "res-3",
            scheduledAt = "2026-11-15T12:00:00+09:00",
            status = "성공",
            cleaningType = "vacuumAndMopTogether"
        )
        assertEquals("vacuumAndMopTogether", itemTogether.cleaningType)
        assertEquals("먼지 + 물청소", itemTogether.getCleaningTypeLabel())
    }

    @Test
    fun testReservationItemDefaultCleaningType() {
        val itemDefault = ReservationItem(
            id = "res-default",
            scheduledAt = "2026-11-15T13:00:00+09:00",
            status = "예정"
        )
        assertEquals(ReservationItem.CLEANING_TYPE_DEFAULT, itemDefault.cleaningType)
        assertEquals("mopAfterVacuum", itemDefault.cleaningType)
        assertEquals("먼지 + 물청소", itemDefault.getCleaningTypeLabel())
    }

    @Test
    fun testCreateAndUpdateResultModelCleaningType() {
        val createSuccess = CreateReservationResult.Success(
            id = "res-c1",
            scheduledAt = "2026-11-15T14:00:00+09:00",
            status = "예정",
            cleaningType = "vacuum"
        )
        assertEquals("vacuum", createSuccess.cleaningType)

        val updateSuccess = UpdateReservationResult.Success(
            id = "res-u1",
            scheduledAt = "2026-11-15T15:00:00+09:00",
            status = "예정",
            cleaningType = "mop"
        )
        assertEquals("mop", updateSuccess.cleaningType)
    }

    @Test
    fun testActivityConstants() {
        assertEquals("EXTRA_CLEANING_TYPE", CreateReservationActivity.EXTRA_CLEANING_TYPE)
        assertEquals("RESULT_CLEANING_TYPE", CreateReservationActivity.RESULT_CLEANING_TYPE)
    }
}
