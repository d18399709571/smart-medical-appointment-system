<template>
  <el-container class="layout-container">
    <el-aside width="200px">
      <div class="logo">医疗后台管理</div>
      <el-menu :default-active="activePath" router>
        <el-menu-item v-if="isSuperadmin" index="/departments">
          <span>科室管理</span>
        </el-menu-item>
        <el-menu-item v-if="isSuperadmin" index="/knowledge">
          <span>知识库管理</span>
        </el-menu-item>
        <el-menu-item v-if="isSuperadmin" index="/admins">
          <span>管理员管理</span>
        </el-menu-item>
        <el-menu-item v-if="isSuperadmin" index="/doctors">
          <span>医生管理</span>
        </el-menu-item>
        <el-menu-item v-if="isSuperadmin" index="/drugs">
          <span>药品目录</span>
        </el-menu-item>
        <el-menu-item v-if="isSuperadmin" index="/ai-monitor">
          <span>AI 运行监控</span>
        </el-menu-item>
        <el-menu-item v-if="isDoctor" index="/my-appointments">
          <span>我的接诊</span>
        </el-menu-item>
        <el-menu-item v-if="isPharmacist" index="/prescriptions">
          <span>待发药处方</span>
        </el-menu-item>
        <el-menu-item v-if="isCashier" index="/bills">
          <span>收费管理</span>
        </el-menu-item>
        <el-menu-item v-if="isLab" index="/reports">
          <span>最终检验报告</span>
        </el-menu-item>
        <el-menu-item index="/profile">
          <span>个人中心</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header>
        <span class="admin-info">{{ currentAdmin?.email }}（{{ roleLabel }}）</span>
        <el-button type="danger" plain size="small" @click="handleLogout">退出登录</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { clearToken, getRefreshToken } from '../../utils/request'
import { logout } from '../../api/auth'
import { currentAdmin, clearCurrentAdmin } from '../../stores/auth'
import { getRoleLabel } from '../../utils/roleAccess'

const route = useRoute()
const router = useRouter()
const activePath = computed(() => route.path)

const isSuperadmin = computed(() => currentAdmin.value?.role === 'superadmin')
const isDoctor = computed(() => currentAdmin.value?.role === 'doctor')
const isPharmacist = computed(() => currentAdmin.value?.role === 'pharmacist')
const isCashier = computed(() => currentAdmin.value?.role === 'cashier')
const isLab = computed(() => currentAdmin.value?.role === 'lab')
const roleLabel = computed(() => getRoleLabel(currentAdmin.value?.role))

async function handleLogout() {
  const refreshToken = getRefreshToken()
  let serverConfirmed = false
  try {
    if (refreshToken) {
      await logout(refreshToken)
      serverConfirmed = true
    }
  } catch {
    // 请求错误已由拦截器提示，仍需完成本地退出。
  } finally {
    clearToken()
    clearCurrentAdmin()
    if (serverConfirmed) {
      ElMessage.success('已退出登录，服务器已确认退出')
    } else {
      ElMessage.warning('已在本地退出，服务器会话撤销未确认')
    }
    await router.push('/login')
  }
}
</script>

<style scoped>
.layout-container {
  height: 100vh;
}
.el-aside {
  background: #304156;
  color: #fff;
}
.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: bold;
  background: #2b3a4e;
}
.el-menu {
  border: none;
  background: #304156;
}
:deep(.el-menu-item) {
  color: #bfcbd9;
}
:deep(.el-menu-item.is-active) {
  background: #263445 !important;
  color: #409eff;
}
:deep(.el-menu-item:hover) {
  background: #263445;
}
.el-header {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  border-bottom: 1px solid #e6e6e6;
}
.admin-info {
  color: #606266;
  font-size: 13px;
}
</style>
