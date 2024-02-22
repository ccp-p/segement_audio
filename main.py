import datetime
import re
from pydub import AudioSegment
import os
# 读取音频文件
audioPath:str = r"D:\project\my_py_project\segement_audio\audioFiles"
outputPath:str = r"D:\project\my_py_project\segement_audio\audioFiles\output"
class myAudioSegment:
    def __init__(self, audioPath):
        self.audioPath = audioPath
        self.aacFiles = self.getAacFileFromAudioPath()
        self.srtFiles = self.getSrtFileFromAudioPath()
        self.accAndSrtArr = self.mergeAacAndSrcToArr()
        self.setAudioField()
        self.timeStamps:list = self.getTimeStampFromSrt(self.getItem('srtFile'))
        
    def setAudioField(self):
        print("加载音频文件中", self.accAndSrtArr)
        for aacAndSrtDict in self.accAndSrtArr:
            if not self.isFileExist(aacAndSrtDict['aacFile'] ):
                return print("音频文件不存在")
            _fileAbsPath = aacAndSrtDict['aacFile']
            _fileSuffix = _fileAbsPath.split(".")[-1]
            audio = AudioSegment.from_file(aacAndSrtDict['aacFile'], format=_fileSuffix)
            aacAndSrtDict['audio'] = audio
        print("音频文件加载成功")

    def isFileExist(self, filePath):
        return os.path.exists(filePath)
    def getFilesFromSuffix(self, suffix:list):
        files = []
        totalFiles = os.listdir(self.audioPath)
        for file in totalFiles:
            # if file includes suffix:
            _suffix = [s.lower() for s in suffix]
            _file = file.lower()
            if os.path.isfile(file) and _file.endswith(tuple(_suffix)):
                absPath = os.path.join(self.audioPath, file)
                files.append(absPath)

        return files
    
    
    def getAacFileFromAudioPath(self):
        os.chdir(self.audioPath)
        aacFiles = self.getFilesFromSuffix([".aac", ".mp3"]) 
        return aacFiles
    def getSrtFileFromAudioPath(self):
        os.chdir(self.audioPath)
        srtFiles = self.getFilesFromSuffix([".srt"])
        return srtFiles

    def mergeAacAndSrcToArr(self):
        accAndSrtArr = []
        aacAndSrtDict = {}
        for aacFile in self.aacFiles:
            for srtFile in self.srtFiles:
                if aacFile.split(".")[0] in srtFile:
                    aacAndSrtDict['aacFile'] = aacFile
                    aacAndSrtDict['srtFile'] = srtFile
                    accAndSrtArr.append(aacAndSrtDict)
                    
        return accAndSrtArr
    
    def getItem(self,name):
        return self.accAndSrtArr[0][name]

    def segment(self):
        print("开始分割音频文件")
        # 对每个时间戳，分割音频文件并合并
        audio_final = AudioSegment.empty()  # 创建一个空的音频片段，用来保存最终的音频
        previous_end_time = 0  # 上一个时间戳的结束时间
        #  self.timeStamps [{'start': datetime.time(1, 5, 30), 'end': datetime.time(1, 5, 30)}]
        #  depend on videos time filter the timestamps arr
        # 1、get the video duration use AudioSegment unit is second
        audio = self.getItem('audio')
        duration = len(audio) / 1000
        print(f"音频文件时长为：{duration}s", duration)
        # 2、filter the timestamps arr ensure the end time is less than the duration

        timeStamps = [time_dict for time_dict in self.timeStamps if self.time_to_seconds(time_dict['end']) < duration]
        for time_dict in timeStamps:
                start_time =  self.time_to_milliseconds(time_dict['start'])

                end_time =  self.time_to_milliseconds(time_dict['end'])
                audio_segment = audio[previous_end_time:start_time]
                audio_final += audio_segment
                previous_end_time = end_time
        audio_final += audio[previous_end_time:]
        self.audio_final = audio_final
    def time_to_milliseconds(self, time):
        return time.hour * 3600000 + time.minute * 60000 + time.second * 1000 + time.microsecond / 1000
    def time_to_seconds(self, time):
        return time.hour * 3600 + time.minute * 60 + time.second
    
    def save(self, savePath=None):
        item = self.getItem('aacFile')
        savePath = savePath if savePath else item.split(".")[0] + "_segement2.mp3"
        self.savePath = savePath
        #  保存音频文件
        
        self.audio_final.export(savePath, format="mp3")
        print("音频文件保存成功")
    def getTimeStampFromSrt(self, srtFile):
        # '00:00:10,533 --> 00:00:11,333\n' start_time = 10, end_time = 11
        startTimeReg= re.compile(r'\d{2}:\d{2}:\d{2},\d{3}')
        endTimesReg = re.compile(r'-->\s(\d{2}:\d{2}:\d{2},\d{3})')
        timestamps = []
        with open(srtFile, 'r',encoding='utf-8') as file:
            for line in file:
                match = startTimeReg.search(line)
                end_match = endTimesReg.search(line)
                time_dict = {}
                if not match or not end_match:
                    continue
                if match:
                    timestamp_str = match.group()
                    # 00:00:10,533 
                    time_dict['start'] = timestamp = datetime.datetime.strptime(timestamp_str, '%H:%M:%S,%f').time()   # to sec
                    # timestamps.append(timestamp)
                if end_match:
                    end_timestamp_str = end_match.group(1)
                    
                    time_dict['end'] = end_timestamp = datetime.datetime.strptime(end_timestamp_str, '%H:%M:%S,%f').time()   # to sec
                    # timestamps.append(end_timestamp)
                timestamps.append(time_dict)
                
        return timestamps

if __name__ == "__main__":
    audio = myAudioSegment(audioPath)
    audio.segment()
    audio.save()
 